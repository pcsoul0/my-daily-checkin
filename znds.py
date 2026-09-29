import re
import time
import os
from datetime import datetime, timezone, timedelta

from logsafe import redact
import requests
from urllib.parse import unquote

# 定义一个更像浏览器的请求头，模拟真实访问
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Cache-Control': 'max-age=0',
}

def decode_response_content(response):
    encoding = response.apparent_encoding or response.encoding or 'utf-8'
    try:
        return response.content.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        for enc in ['utf-8', 'gbk', 'gb2312']:
            try:
                return response.content.decode(enc)
            except UnicodeDecodeError:
                continue
        print(f"[WARNING] 无法使用标准编码解码响应，将使用 'utf-8' 并忽略错误。")
        return response.content.decode('utf-8', errors='ignore')


def get_formhash_and_login(session, username, password):
    """获取 formhash 并执行登录"""
    login_url = "https://www.znds.com/member.php?mod=logging&action=login&referer="
    print(f"[DEBUG] 正在访问登录页面: {login_url}")
    
    response = session.get(login_url, headers=HEADERS)
    print(f"[DEBUG] 登录页面响应状态码: {response.status_code}")

    if response.status_code != 200:
        print(f"[ERROR] 访问登录页面失败，状态码: {response.status_code}")
        print(f"[ERROR] 页面内容: {redact(response.text)[:500]}")
        raise Exception(f"登录失败：访问登录页面失败，状态码 {response.status_code}")

    # 尝试多种可能的 formhash 模式
    patterns = [
        r'name="formhash"\s+value="([a-zA-Z0-9]+)"',
        r'name=\'formhash\'\s+value=\'([a-zA-Z0-9]+)\'',
        r'name=formhash[^>]*value=([a-zA-Z0-9]+)',
        r'name="formhash"[^>]*value="([a-zA-Z0-9]+)"',
    ]
    
    formhash = None
    for pattern in patterns:
        match = re.search(pattern, response.text)
        if match:
            formhash = match.group(1)
            break
    
    if not formhash:
        print("[ERROR] 无法从登录页面获取 formhash。页面HTML内容如下:")
        print(redact(response.text)[:1000])
        raise Exception("登录失败：无法获取 formhash")
    
    print(f"[INFO] 成功获取到登录页面的 formhash: {formhash}")

    # 构建登录数据
    login_data = {
        'formhash': formhash,
        'referer': 'https://www.znds.com/',
        'loginfield': 'username',
        'username': username,
        'password': password,
        'questionid': '0',
        'answer': '',
        'cookietime': '2592000'
    }

    login_headers = HEADERS.copy()
    login_headers.update({
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': login_url,
    })

    login_post_url = f"https://www.znds.com/member.php?mod=logging&action=login&loginsubmit=yes&loginhash=LqZ1B&inajax=1"
    print(f"[DEBUG] 正在发送登录请求至: {login_post_url}")
    session.post(login_post_url, data=login_data, headers=login_headers)

    # 登录后，尝试访问一个需要登录的页面来验证登录状态
    profile_url = "https://www.znds.com/home.php?mod=spacecp&ac=profile"
    print(f"[DEBUG] 尝试访问需要登录的页面以验证身份: {profile_url}")
    verify_response = session.get(profile_url, headers=HEADERS)
    verify_decoded_text = decode_response_content(verify_response)
    
    if "您还没有登录" in verify_decoded_text or "请登录后再使用" in verify_decoded_text or "login" in verify_response.url:
        print(f"[ERROR] 登录验证失败。访问个人中心页面被重定向或提示未登录。URL: {verify_response.url}")
                # 已登录态页面正文含账号标识，只报长度不打印正文（公开仓库日志安全）
        print(f"[ERROR] 页面长度: {len(verify_decoded_text)} 字符（正文含账号信息，已省略）")
        raise Exception("登录失败：请检查用户名和密码是否正确。")
        
    print("[INFO] 登录验证成功")


def get_formhash_from_page(html_content):
    """
    从页面 HTML 内容中查找 formhash
    优先级：1. <input> 标签, 2. JavaScript 变量, 3. URL 参数 (如 onclick), 4. 锚点链接 (a href)
    """
    # 1. 尝试从 <input> 标签中查找
    formhash_input_pattern = r'name=["\']?formhash["\']?\s+value=["\']?([a-zA-Z0-9]+)["\']?'
    match = re.search(formhash_input_pattern, html_content)
    if match:
        return match.group(1)
    
    # 2. 尝试从 JavaScript 变量中查找 (var formhash = 'xxx';)
    js_formhash_pattern = r'var\s+formhash\s*=\s*["\']([a-zA-Z0-9]+)["\']'
    match = re.search(js_formhash_pattern, html_content)
    if match:
        return match.group(1)
        
    # 3. 尝试从全局 JS 对象中查找 (e.g., creditnotice..., formhash: 'xxx', ...)
    global_js_pattern = r'formhash["\']?\s*:\s*["\']([a-zA-Z0-9]+)["\']'
    match = re.search(global_js_pattern, html_content)
    if match:
        return match.group(1)

    # 4. 尝试从 URL 参数中查找 (例如在 onclick 属性里)
    url_param_pattern_onclick = r'onclick\s*=\s*["\'][^"\']*formhash=([a-zA-Z0-9]+)[^"\']*["\']'
    match = re.search(url_param_pattern_onclick, html_content)
    if match:
        return match.group(1)

    # 5. 尝试从 <a href=""> 标签的 URL 中查找
    url_param_pattern_href = r'<a\s+[^>]*href\s*=\s*["\'][^"\']*formhash=([a-zA-Z0-9]+)[^"\']*["\'][^>]*>'
    match = re.search(url_param_pattern_href, html_content)
    if match:
        return match.group(1)

    return None


def sign_in_and_get_credit_info(session):
    """执行签到并获取积分信息"""
    print(f"[DEBUG] 正在访问论坛主页 (forum.php) 以获取 formhash...")
    # 访问登录后的论坛首页
    forum_home_url = "https://www.znds.com/forum.php"
    home_response = session.get(forum_home_url, headers=HEADERS)
    home_decoded_text = decode_response_content(home_response)
    
    # 使用更新后的函数来查找 formhash
    formhash = get_formhash_from_page(home_decoded_text)

    if not formhash:
         print(f"[ERROR] 无法在论坛主页 (forum.php) 获取 formhash。页面HTML内容如下:")
         print(f"[ERROR] 页面长度: {len(home_decoded_text)} 字符（正文含账号信息，已省略）")
         raise Exception("签到失败：无法在论坛主页获取 formhash")
    
    print(f"[INFO] 从论坛主页 (forum.php) 获取到最新的 formhash: {formhash}")
         
    sign_url = f"https://www.znds.com/plugin.php?id=ljdaka%3Adaka&action=msg&formhash={formhash}&infloat=yes&handlekey=ljdaka&inajax=1&ajaxtarget=fwin_content_ljdaka"
    sign_headers = HEADERS.copy()
    sign_headers.update({
        'Accept': '*/*',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': 'https://www.znds.com/forum.php',
    })
    
    print(f"[DEBUG] 正在发送签到请求至: {sign_url}")
    sign_response = session.get(sign_url, headers=sign_headers)
    sign_decoded_text = decode_response_content(sign_response)
    
    # 尝试从响应中提取签到信息
    msg_match = re.search(r'"alert_info"[^>]*>.*?<p>(.*?)</p>', sign_decoded_text, re.S)
    msg = msg_match.group(1).strip() if msg_match else "未知"
    print(f"[INFO] 签到信息: {redact(msg)}")

    # 获取积分信息
    print(f"[DEBUG] 正在获取积分信息...")
    credit_url = "https://www.znds.com/home.php?mod=spacecp&ac=credit"
    credit_response = session.get(credit_url, headers=HEADERS)
    credit_decoded_text = decode_response_content(credit_response)

    # 使用正则表达式从积分页面提取各项数值
    jb_match = re.search(r'金币:\s*</em>\s*(\d+)', credit_decoded_text)
    ww_match = re.search(r'威望:\s*</em>\s*(\d+)', credit_decoded_text)
    zb_match = re.search(r'Z币:\s*</em>\s*(\d+)', credit_decoded_text)
    jf_match = re.search(r'积分:\s*</em>\s*(\d+)', credit_decoded_text)

    jb = jb_match.group(1) if jb_match else "N/A"
    ww = ww_match.group(1) if ww_match else "N/A"
    zb = zb_match.group(1) if zb_match else "N/A"
    jf = jf_match.group(1) if jf_match else "N/A"

    print(f"[INFO] 金币: {jb}, 威望: {ww}, Z币: {zb}, 积分: {jf}")

    return msg, jb, ww, zb, jf

# 写入签到结果到 checkin_results.txt（追加写入，保留原有内容）
def write_result(content):
    try:
        # 强制使用北京时间（UTC+8）
        beijing_tz = timezone(timedelta(hours=8))
        now = datetime.now(beijing_tz).strftime("%Y-%m-%d %H:%M:%S")
        # 构建带时间戳的日志行，并在末尾加换行符，保证每条占一行
        log_line = f"{now} - {content}\n\n"
        
        # 追加写入 checkin_results.txt，确保不会覆盖原有内容
        with open("checkin_results.txt", "a", encoding="utf-8") as f:
            f.write(log_line)
        
        print("[INFO] 签到结果已成功追加写入 checkin_results.txt")
    except Exception as e:
        print(f"[ERROR] 写入 checkin_results.txt 失败: {str(e)}")

def main():
    username = os.getenv('ZNDS_USERNAME')
    password = os.getenv('ZNDS_PASSWORD')

    if not username or not password:
        print("[ERROR] 缺少必要的环境变量 ZNDS_USERNAME 或 ZNDS_PASSWORD")
        exit(1)

    session = requests.Session()

    try:
        get_formhash_and_login(session, username, password)
        msg, jb, ww, zb, jf = sign_in_and_get_credit_info(session)
        # 日志内容只写业务信息，时间戳交给 write_result 处理
        log_message = f"智能电视网 签到成功 | 结果：{msg} | 金币：{jb} | 威望：{ww} | Z币：{zb} | 积分：{jf}"
        print(log_message)
        
        # 写入 checkin_results.txt
        write_result(log_message)

    except Exception as e:
        error_msg = f"智能电视网 签到失败 | 原因：{str(e)}"
        print(error_msg)
        
        # 失败也写入日志
        write_result(error_msg)

if __name__ == "__main__":
    main()
