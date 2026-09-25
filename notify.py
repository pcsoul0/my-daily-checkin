import requests
import os

def send(title, content):
    """PushPlus推送函数"""
    # 从GitHub Action环境变量获取PushPlus令牌
    pushplus_token = os.getenv("PUSHPLUS_TOKEN")
    if not pushplus_token:
        print("未配置PushPlus令牌，跳过推送")
        return False
    
    url = "http://www.pushplus.plus/send"
    data = {
        "token": pushplus_token,
        "title": title,
        "content": content,
        "template": "txt"  # 文本格式，可选markdown
    }
    try:
        response = requests.post(url, json=data, timeout=10)
        response.raise_for_status()
        print(f"PushPlus推送结果：{response.json()}")
        return True
    except Exception as e:
        print(f"PushPlus推送失败：{str(e)}")
        return False

# 保持原脚本的调用兼容（原脚本会导入send函数）
if __name__ == "__main__":
    # 测试用（可选）
    send("测试标题", "测试内容")
