
"""
文件服务客户端
"""
import os
import requests
import mimetypes
from config import FILE_STORE_CONFIG

class FileClient:
    """文件服务客户端"""
    
    def __init__(self):
        self.server_url = FILE_STORE_CONFIG['access_url']
        
    def upload(self, bucket: str, file_path: str, is_cache: bool = False) -> dict:
        """
        上传文件
        
        Args:
            bucket: 存储桶名称
            file_path: 文件路径
            is_cache: 是否缓存
            
        Returns:
            dict: {
                'url': 文件URL,
                'bucket': 存储桶名称,
                'objectKey': 对象键名
            }
            
        Raises:
            Exception: 上传失败时抛出异常
        """
        try:
            url = f"{self.server_url}/upload/{bucket}"
            
            # 获取文件MIME类型
            mime_type, _ = mimetypes.guess_type(file_path)
            # 打印文件扩展名和MIME类型
            print(f"文件扩展名: {os.path.splitext(file_path)[1].lower()}")
            print(f"MIME类型: {mime_type}")
            if not mime_type:
                # 根据文件扩展名设置MIME类型
                ext = os.path.splitext(file_path)[1].lower()
                mime_map = {
                    '.tif': 'image/tiff',
                    '.tiff': 'image/tiff',
                    '.png': 'image/png',
                    '.jpg': 'image/jpeg',
                    '.jpeg': 'image/jpeg',
                    '.gif': 'image/gif',
                    '.bmp': 'image/bmp',
                    '.pdf': 'application/pdf',
                    '.json': 'application/json',
                    '.txt': 'text/plain'
                }
                mime_type = mime_map.get(ext, 'application/octet-stream')
            
            # 读取文件
            with open(file_path, 'rb') as f:
                files = {
                    'file': (os.path.basename(file_path), f, mime_type)
                }
                data = {'is_cache': is_cache}
                
                # 发送请求
                response = requests.post(url, files=files, data=data)
                
            # 检查响应
            if response.status_code == 200:
                result = response.json()
                if result['code'] == 200:
                    return result['data']
                raise Exception(result['msg'])
            raise Exception(f"上传失败: {response.status_code}")
            
        except Exception as e:
            raise Exception(f"上传文件失败: {str(e)}")
            
    def delete(self, bucket: str, object_key: str) -> None:
        """
        删除文件
        
        Args:
            bucket: 存储桶名称
            object_key: 对象键名
            
        Raises:
            Exception: 删除失败时抛出异常
        """
        try:
            url = f"{self.server_url}/{bucket}/{object_key}"
            
            # 发送请求
            response = requests.delete(url)
            
            # 检查响应
            if response.status_code == 200:
                result = response.json()
                if result['code'] == 200:
                    return
                raise Exception(result['msg'])
            raise Exception(f"删除失败: {response.status_code}")
            
        except Exception as e:
            raise Exception(f"删除文件失败: {str(e)}")
            
    def get(self, bucket: str, object_key: str) -> bytes:
        """
        获取文件
        
        Args:
            bucket: 存储桶名称
            object_key: 对象键名
            
        Returns:
            bytes: 文件内容
            
        Raises:
            Exception: 获取失败时抛出异常
        """
        try:
            url = f"{self.server_url}/{bucket}/{object_key}"
            
            # 发送请求
            response = requests.get(url)
            
            # 检查响应
            if response.status_code == 200:
                return response.content
            raise Exception(f"获取失败: {response.status_code}")
            
        except Exception as e:
            raise Exception(f"获取文件失败: {str(e)}")
            
    def get_file_url(self, bucket: str, object_key: str) -> str:
        """
        获取文件URL
        
        Args:
            bucket: 存储桶名称
            object_key: 对象键名
            
        Returns:
            str: 文件URL
        """
        return f"{self.server_url}/{bucket}/{object_key}"
        
    def download(self, bucket: str, object_key: str, save_path: str) -> None:
        """
        下载文件
        
        Args:
            bucket: 存储桶名称
            object_key: 对象键名
            save_path: 保存路径
            
        Raises:
            Exception: 下载失败时抛出异常
        """
        try:
            # 获取文件内容
            content = self.get(bucket, object_key)
            
            # 创建目录
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            
            # 保存文件
            with open(save_path, 'wb') as f:
                f.write(content)
                
        except Exception as e:
            raise Exception(f"下载文件失败: {str(e)}")

# 创建全局实例
file_client = FileClient() 