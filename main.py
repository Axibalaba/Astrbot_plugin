from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api import AstrBotConfig
import httpx
import json
import asyncio
import os
import random
import time
from typing import List, Dict, Set, Optional
from PIL import Image as PILImage, ImageDraw, ImageFont
import aiofiles
import tempfile
import shutil

@register("图片响应插件", "AI Assistant", "基于关键词触发的图片响应插件", "1.0.0")
class ImageResponsePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        
        # 默认配置
        self.default_keywords = ["导管", "飞机", "A片", "打胶", "刺刀", "造人", "女优", "色图", "涩图"]
        self.default_text = "给你一张图片："
        
        # 从配置中读取设置
        self.keywords = self.config.get("keywords", self.default_keywords)
        self.custom_text = self.config.get("custom_text", self.default_text)
        self.at_user = self.config.get("at_user", True)
        self.selected_txt_files = self.config.get("selected_txt_files", [])  # 空列表表示使用所有文件
        self.watermark_text = self.config.get("watermark_text", "")
        self.watermark_font = self.config.get("watermark_font", "")
        self.show_avatar = self.config.get("show_avatar", False)
        self.local_image_dir = self.config.get("local_image_dir", "")
        self.external_api = self.config.get("external_api", "https://api.lolicon.app/setu/v2?r18=1")
        
        # 图片缓存配置
        self.cache_duration = self.config.get("cache_duration", 300)  # 默认5分钟缓存
        self.image_cache = {}
        
        # 初始化目录路径
        self.data_dir = os.path.dirname(os.path.abspath(__file__))
        self.tu_dir = os.path.join(self.data_dir, "tu")
        self.font_dir = os.path.join(self.data_dir, "font")
        self.avatar_dir = os.path.join(self.data_dir, "avatars")
        self.temp_dir = os.path.join(self.data_dir, "temp")  # 添加专用临时目录
        
        # 创建必要的目录
        os.makedirs(self.tu_dir, exist_ok=True)
        os.makedirs(self.font_dir, exist_ok=True)
        os.makedirs(self.avatar_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)  # 创建临时目录
        
        # 设置临时文件的目录
        tempfile.tempdir = self.temp_dir
        
        # 并发控制
        self.semaphore = asyncio.Semaphore(5)
        
        # HTTP客户端
        self._http_timeout = httpx.Timeout(30.0)
        self._connection_limit = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=10))
        
        # 清理旧的临时文件
        self._clean_old_temp_files()
        
        # 图片去重机制 - 记录1小时内发送过的图片
        self.sent_images = {}  # {image_url: timestamp}
        self.sent_images_timeout = 3600  # 1小时超时（秒）
        
    async def _get_http_client(self):
        return httpx.AsyncClient(timeout=self._http_timeout, transport=self._connection_limit)
    
    # 关键词消息处理器
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_handler(self, event: AstrMessageEvent):
        """处理包含关键词的消息"""
        message_str = event.message_str.strip().lower()
        
        # 检查消息是否包含任何关键词
        for keyword in self.keywords:
            if keyword.lower() in message_str:
                logger.info(f"检测到关键词: {keyword}")
                async for result in self.handle_image_response(event, keyword):
                    yield result
                break
    
    # 命令处理器 - 用于管理插件
    @filter.command("image_help")
    async def help_command(self, event: AstrMessageEvent):
        """显示插件帮助信息"""
        help_text = f"""
        **图片响应插件帮助**
        
        **功能说明:**
        - 当消息中包含预设关键词时，自动发送相关图片
        - 支持自定义关键词、回复文本、水印等设置
        
        **当前配置:**
        - 触发关键词: {', '.join(self.keywords)}
        - 回复文本: {self.custom_text}
        - @用户功能: {'开启' if self.at_user else '关闭'}
        - 水印功能: {'开启' if self.watermark_text else '关闭'}
        - 显示头像: {'开启' if self.show_avatar else '关闭'}
        
        **注意事项:**
        - 插件有5分钟图片缓存机制
        - 可通过后台管理界面修改所有配置
        """
        yield event.plain_result(help_text)
    
    # 命令处理器 - 刷新关键词
    @filter.command("image_reload")
    async def reload_command(self, event: AstrMessageEvent):
        """重新加载配置"""
        try:
            # 重新读取配置
            self.keywords = self.config.get("keywords", self.default_keywords)
            self.custom_text = self.config.get("custom_text", self.default_text)
            self.at_user = self.config.get("at_user", True)
            self.selected_txt_files = self.config.get("selected_txt_files", [])
            self.watermark_text = self.config.get("watermark_text", "")
            self.watermark_font = self.config.get("watermark_font", "")
            self.show_avatar = self.config.get("show_avatar", False)
            self.local_image_dir = self.config.get("local_image_dir", "")
            self.external_api = self.config.get("external_api", "https://api.lolicon.app/setu/v2?r18=0")
            
            # 清空缓存
            self.image_cache.clear()
            
            yield event.plain_result(f"配置已重新加载！\n当前关键词: {', '.join(self.keywords)}")
        except Exception as e:
            logger.error(f"重新加载配置失败: {e}")
            yield event.plain_result(f"重新加载配置失败: {str(e)}")
    
    async def handle_image_response(self, event: AstrMessageEvent, keyword: str):
        """处理图片响应的核心逻辑"""
        user_id = event.get_sender_id()
        
        try:
            logger.info(f"为用户 {user_id} 处理关键词 '{keyword}' 的图片响应")
            # 尝试获取图片
            image_path = await self.get_image(keyword)
            if not image_path:
                logger.warning(f"未能为关键词 '{keyword}' 获取图片")
                yield event.plain_result("抱歉，未能找到合适的图片。")
                return
            logger.info(f"成功获取图片: {image_path}")
            
            # 添加水印（如果配置）
            if self.watermark_text:
                try:
                    image_path = await self.add_watermark(image_path)
                except Exception as e:
                    logger.error(f"添加水印失败: {e}")
            
            # 准备回复消息链
            chain = []
            
            # 添加@用户（如果配置）
            if self.at_user:
                chain.append(At(qq=user_id))
            
            # 添加自定义文本
            if self.custom_text:
                chain.append(Plain(self.custom_text))
            
            # 添加图片
            chain.append(Image(file=image_path))
            
            # 发送消息
            yield event.chain_result(chain)
            
        except Exception as e:
            logger.error(f"处理图片响应时出错: {e}")
            yield event.plain_result(f"处理图片时发生错误: {str(e)}")
    
    async def get_image(self, keyword: str) -> Optional[str]:
        """获取图片的主要逻辑 - 确保每次都完全重新选择图片"""
        logger.info(f"开始为关键词 '{keyword}' 获取新图片")
        # 完全跳过缓存检查，确保每次都获取新的随机图片
        # 但仍保留1小时内图片URL去重功能
        async with self.semaphore:
            # 1. 尝试根据关键词匹配特定的TXT文件
            image_path = await self._get_image_from_specific_txt(keyword)
            if image_path:
                await self._add_to_cache(keyword, image_path)
                return image_path
            
            # 2. 尝试从配置的TXT文件中获取
            image_path = await self._get_image_from_configured_txt()
            if image_path:
                await self._add_to_cache(keyword, image_path)
                return image_path
            
            # 3. 尝试从本地图片目录获取
            if self.local_image_dir and os.path.exists(self.local_image_dir):
                image_path = await self._get_image_from_local_dir()
                if image_path:
                    await self._add_to_cache(keyword, image_path)
                    return image_path
            
            # 4. 尝试从外部API获取
            image_path = await self._get_image_from_api()
            if image_path:
                await self._add_to_cache(keyword, image_path)
                return image_path
            
            return None
    
    async def _get_image_from_specific_txt(self, keyword: str) -> Optional[str]:
        """从与关键词匹配的TXT文件中获取图片"""
        try:
            # 检查tu目录是否存在
            if not os.path.exists(self.tu_dir):
                logger.warning(f"目录不存在: {self.tu_dir}")
                return None
                
            txt_files = [f for f in os.listdir(self.tu_dir) if f.endswith('.txt')]
            
            # 寻找与关键词匹配的文件名（不包含.txt后缀）
            for txt_file in txt_files:
                file_name = os.path.splitext(txt_file)[0]
                if keyword.lower() == file_name.lower():
                    file_path = os.path.join(self.tu_dir, txt_file)
                    logger.info(f"找到匹配的TXT文件: {file_path}")
                    return await self._get_random_image_from_file(file_path)
            
            logger.info(f"未找到与关键词 '{keyword}' 匹配的TXT文件")
        except Exception as e:
            logger.error(f"从特定TXT文件获取图片失败: {e}")
        return None
    
    async def _get_image_from_configured_txt(self) -> Optional[str]:
        """从配置的TXT文件中获取图片"""
        try:
            # 检查tu目录是否存在
            if not os.path.exists(self.tu_dir):
                logger.warning(f"目录不存在: {self.tu_dir}")
                return None
                
            all_txt_files = [f for f in os.listdir(self.tu_dir) if f.endswith('.txt')]
            
            # 确定要使用的TXT文件列表
            txt_files_to_use = []
            if self.selected_txt_files:
                # 使用配置的特定文件，支持绝对路径
                for selected_file in self.selected_txt_files:
                    # 检查是否为绝对路径
                    if os.path.isabs(selected_file):
                        if os.path.exists(selected_file):
                            txt_files_to_use.append(selected_file)
                            logger.info(f"添加绝对路径文件: {selected_file}")
                        else:
                            logger.warning(f"配置的绝对路径文件不存在: {selected_file}")
                    else:
                        # 相对路径，在tu目录中查找
                        full_name = f"{selected_file}.txt" if not selected_file.endswith('.txt') else selected_file
                        if full_name in all_txt_files:
                            txt_files_to_use.append(os.path.join(self.tu_dir, full_name))
            else:
                # 使用所有文件
                txt_files_to_use = [os.path.join(self.tu_dir, f) for f in all_txt_files]
            
            if not txt_files_to_use:
                logger.info("没有可用的TXT文件")
                return None
            
            # 随机选择一个文件
            selected_file = random.choice(txt_files_to_use)
            logger.info(f"随机选择的TXT文件: {selected_file}")
            
            return await self._get_random_image_from_file(selected_file)
        except Exception as e:
            logger.error(f"从配置的TXT文件获取图片失败: {e}")
        return None
    
    async def _get_random_image_from_file(self, file_path: str) -> Optional[str]:
        """从指定的TXT文件中随机选择一个图片URL并下载，实现1小时内去重"""
        try:
            logger.info(f"正在从文件 {file_path} 随机选择图片")
            
            # 每次都重新读取文件，确保获取最新的URL列表
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                lines = [line.strip() for line in await f.readlines() if line.strip()]
            
            if not lines:
                logger.warning("文件中没有有效图片URL")
                return None
            
            logger.info(f"找到 {len(lines)} 个图片URL")
            
            # 清理过期的已发送图片记录
            current_time = time.time()
            self._clean_expired_sent_images(current_time)
            
            # 创建未发送图片列表（1小时内未发送过的图片）
            available_urls = [url for url in lines if url not in self.sent_images]
            
            # 如果所有图片都在1小时内发送过，则允许重复
            if not available_urls:
                logger.warning(f"所有 {len(lines)} 个图片URL在1小时内都已发送过，将允许重复发送")
                available_urls = lines.copy()
            else:
                logger.info(f"有 {len(available_urls)} 个URL在1小时内未发送过")
            
            # 确保真正随机选择一个可用的URL
            random.shuffle(available_urls)  # 先打乱顺序
            image_url = random.choice(available_urls)
            logger.info(f"随机选择的图片URL: {image_url}")
            
            # 下载图片
            image_path = await self._download_image(image_url)
            if image_path:
                # 记录已发送图片（使用URL作为标识）
                self.sent_images[image_url] = current_time
                logger.info(f"记录已发送图片: {image_url}")
            
            return image_path
        except Exception as e:
            logger.error(f"从文件获取随机图片失败: {e}")
        return None
    
    async def _get_image_from_local_dir(self) -> Optional[str]:
        """从本地图片目录获取图片"""
        try:
            image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp'}
            image_files = [
                f for f in os.listdir(self.local_image_dir)
                if os.path.splitext(f)[1].lower() in image_extensions
            ]
            
            if not image_files:
                return None
            
            # 随机选择一个图片
            selected_image = random.choice(image_files)
            return os.path.join(self.local_image_dir, selected_image)
        except Exception as e:
            logger.error(f"从本地目录获取图片失败: {e}")
        return None
    
    async def _get_image_from_api(self) -> Optional[str]:
        """从外部API获取图片"""
        try:
            async with await self._get_http_client() as client:
                response = await client.get(self.external_api)
                response.raise_for_status()
                
                data = response.json()
                
                # 处理不同API的响应格式
                if 'data' in data and isinstance(data['data'], list) and data['data']:
                    # 兼容lolicon.app API
                    image_info = data['data'][0]
                    if 'urls' in image_info and 'original' in image_info['urls']:
                        image_url = image_info['urls']['original']
                    elif 'url' in image_info:
                        image_url = image_info['url']
                    else:
                        return None
                elif 'url' in data:
                    # 直接返回URL
                    image_url = data['url']
                else:
                    return None
                
                return await self._download_image(image_url)
        except Exception as e:
            logger.error(f"从API获取图片失败: {e}")
        return None
    
    async def _download_image(self, url: str) -> Optional[str]:
        """下载图片并返回本地路径"""
        try:
            # 创建临时文件，使用更清晰的命名规则
            timestamp = int(time.time() * 1000)
            random_id = random.randint(1000, 9999)
            
            # 获取文件扩展名
            ext = os.path.splitext(url)[1] or '.jpg'
            if ext.startswith('?'):
                ext = '.jpg'
            
            # 使用自定义临时文件名，确保在临时目录内
            temp_filename = f"img_{timestamp}_{random_id}{ext}"
            temp_path = os.path.join(self.temp_dir, temp_filename)
            
            # 确保临时目录存在
            os.makedirs(self.temp_dir, exist_ok=True)
            
            async with await self._get_http_client() as client:
                response = await client.get(url)
                response.raise_for_status()
                
                async with aiofiles.open(temp_path, 'wb') as f:
                    await f.write(response.content)
                
            logger.info(f"图片下载成功: {temp_path}")
            return temp_path
        except Exception as e:
            logger.error(f"下载图片失败: {e}")
        return None
    
    async def add_watermark(self, image_path: str) -> str:
        """为图片添加水印"""
        try:
            # 使用asyncio.to_thread避免阻塞事件循环
            return await asyncio.to_thread(self._add_watermark_sync, image_path)
        except Exception as e:
            logger.error(f"添加水印失败: {e}")
            return image_path
    
    def _add_watermark_sync(self, image_path: str) -> str:
        """同步添加水印的方法"""
        with PILImage.open(image_path) as img:
            # 创建绘制对象
            draw = ImageDraw.Draw(img)
            
            # 尝试加载指定字体或使用默认字体
            try:
                if self.watermark_font:
                    font_path = os.path.join(self.font_dir, self.watermark_font)
                    if os.path.exists(font_path):
                        font_size = max(10, min(30, img.height // 20))
                        font = ImageFont.truetype(font_path, font_size)
                    else:
                        font = ImageFont.load_default()
                else:
                    font = ImageFont.load_default()
            except:
                font = ImageFont.load_default()
            
            # 计算水印位置（右下角）
            text = self.watermark_text
            # 获取文本边界框
            try:
                bbox = draw.textbbox((0, 0), text, font=font)
                text_width = bbox[2] - bbox[0]
                text_height = bbox[3] - bbox[1]
            except:
                # 降级处理
                text_width, text_height = 100, 20
            
            # 水印位置：右下角，带边距
            margin = 20
            x = img.width - text_width - margin
            y = img.height - text_height - margin
            
            # 绘制半透明水印
            # 先绘制半透明背景
            draw.rectangle(
                [(x - 5, y - 5), (x + text_width + 5, y + text_height + 5)],
                fill=(255, 255, 255, 100)
            )
            # 绘制文字
            draw.text((x, y), text, font=font, fill=(0, 0, 0, 180))
            
            # 保存带水印的图片
            watermarked_path = image_path + "_watermarked" + os.path.splitext(image_path)[1]
            img.save(watermarked_path)
            
            return watermarked_path
    
    async def _get_from_cache(self, keyword: str) -> Optional[str]:
        """从缓存获取图片"""
        current_time = time.time()
        if keyword in self.image_cache:
            cached_time, image_path = self.image_cache[keyword]
            # 检查是否过期
            if current_time - cached_time < self.cache_duration:
                # 检查文件是否存在
                if os.path.exists(image_path):
                    return image_path
                else:
                    # 文件不存在，从缓存中移除
                    del self.image_cache[keyword]
        
        # 清理过期缓存
        self._clean_cache(current_time)
        return None
    
    async def _add_to_cache(self, keyword: str, image_path: str):
        """添加图片到缓存 - 已修改为不进行缓存，确保每次都获取新图片"""
        # 不进行缓存，确保每次都获取新的随机图片
        # 保留该方法以保持代码结构完整性，但不执行实际缓存操作
        pass
    
    def _clean_cache(self, current_time: float):
        """清理过期缓存"""
        expired_keys = [
            key for key, (cached_time, _) in self.image_cache.items()
            if current_time - cached_time >= self.cache_duration
        ]
        for key in expired_keys:
            # 尝试删除临时文件
            try:
                _, image_path = self.image_cache[key]
                if os.path.exists(image_path):
                    os.remove(image_path)
                    logger.info(f"清理过期缓存图片: {image_path}")
            except Exception as e:
                logger.error(f"删除过期缓存图片失败: {e}")
            del self.image_cache[key]
            
    def _clean_expired_sent_images(self, current_time: float):
        """清理过期的已发送图片记录"""
        expired_urls = [
            url for url, timestamp in self.sent_images.items()
            if current_time - timestamp > self.sent_images_timeout
        ]
        for url in expired_urls:
            self.sent_images.pop(url, None)
            logger.info(f"清理过期的已发送图片记录: {url}")
            
    def _clean_old_temp_files(self):
        """清理旧的临时文件"""
        try:
            if not os.path.exists(self.temp_dir):
                return
                
            current_time = time.time()
            # 清理24小时前的临时文件
            max_age = 24 * 3600  # 24小时
            
            for filename in os.listdir(self.temp_dir):
                file_path = os.path.join(self.temp_dir, filename)
                if os.path.isfile(file_path):
                    file_age = current_time - os.path.getmtime(file_path)
                    if file_age > max_age:
                        try:
                            os.remove(file_path)
                            logger.info(f"清理旧临时文件: {file_path}")
                        except Exception as e:
                            logger.error(f"删除旧临时文件失败: {e}")
        except Exception as e:
            logger.error(f"清理临时文件时出错: {e}")
    
    def __del__(self):
        """清理资源"""
        # 清理所有缓存的临时文件
        try:
            for _, image_path in self.image_cache.values():
                if os.path.exists(image_path):
                    os.remove(image_path)
                    logger.info(f"程序退出时清理缓存图片: {image_path}")
        except Exception as e:
            logger.error(f"程序退出时清理缓存图片失败: {e}")
        
        # 清理临时目录中的所有文件
        try:
            if os.path.exists(self.temp_dir):
                shutil.rmtree(self.temp_dir)
                logger.info(f"程序退出时清理临时目录: {self.temp_dir}")
        except Exception as e:
            logger.error(f"程序退出时清理临时目录失败: {e}")