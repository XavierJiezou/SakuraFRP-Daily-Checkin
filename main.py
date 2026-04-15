import sys
import os
import io
import time
import random
import re
import json
import argparse
import traceback
import socket
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
from PIL import Image
from playwright.sync_api import sync_playwright
from ai_service import AIService
from logger import CheckinLogger
import pytweening  # 用于缓动函数（无GUI依赖）

# 加载环境变量
load_dotenv()

# 强制 Windows 终端使用 UTF-8 编码
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')

# ========= 配置 =========
BASE_DIR = Path(__file__).resolve().parent
domain = "www.natfrp.com"
target_url = f"https://{domain}/user/"

ACCOUNT_FILE = BASE_DIR / "account.txt"  
STATE_FILE = BASE_DIR / "state.json"     
SUCCESS_SCREENSHOT = BASE_DIR / "checkin.png"
ALERT_STATE_FILE = BASE_DIR / "alert_state.json"

ALREADY_SIGNED_TEXT = "今天已经签到过啦"       
SIGNED_ANCESTOR_LEVELS = 3                

# ---------------- 工具函数 ----------------
def load_file_content(path: Path):
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    return path.read_text(encoding="utf-8").strip()

def load_username_password(path: Path):
    content = load_file_content(path)
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    if len(lines) < 2:
        raise ValueError("account.txt 格式错误：需两行分别存放用户名和密码")
    return lines[0], lines[1]

def clean_old_logs(base_dir: Path, days: int = 30):
    """清理指定天数前的日志文件"""
    logs_dir = base_dir / "logs"
    if not logs_dir.exists():
        return
    
    # 计算截止日期（30天前）
    cutoff_date = datetime.now() - timedelta(days=days)
    
    # 遍历logs目录下的所有文件
    deleted_count = 0
    for log_file in logs_dir.iterdir():
        if not log_file.is_file():
            continue
        
        # 检查是否是日志文件（格式：checkin_YYYY-MM-DD.log）
        if log_file.name.startswith("checkin_") and log_file.name.endswith(".log"):
            try:
                # 从文件名提取日期（checkin_YYYY-MM-DD.log）
                date_str = log_file.name.replace("checkin_", "").replace(".log", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                
                # 如果文件日期早于截止日期，删除文件
                if file_date < cutoff_date:
                    log_file.unlink()
                    deleted_count += 1
                    print(f"[INFO] 已删除旧日志文件: {log_file.name} (日期: {date_str})")
            except ValueError:
                # 如果文件名格式不正确，跳过
                continue
    
    if deleted_count > 0:
        print(f"[INFO] 清理完成，共删除 {deleted_count} 个30天前的日志文件")

def parse_traffic_to_bytes(value_text: str):
    """将流量字符串（如 1.23 GB / 512MB）转换为字节数"""
    if not value_text:
        return None

    text = value_text.strip().replace(" ", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)", text, re.IGNORECASE)
    if not match:
        return None

    num = float(match.group(1))
    unit = match.group(2).upper()
    multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024 ** 2,
        "GB": 1024 ** 3,
        "TB": 1024 ** 4,
        "KIB": 1024,
        "MIB": 1024 ** 2,
        "GIB": 1024 ** 3,
        "TIB": 1024 ** 4,
    }
    return int(num * multipliers.get(unit, 1))

def format_traffic(bytes_value):
    """将字节数格式化为可读流量字符串"""
    if bytes_value is None:
        return "未知"

    value = float(bytes_value)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    return f"{value:.2f} {units[unit_index]}"

def get_remaining_traffic_bytes(page, logger=None):
    """从页面文本中提取“剩余/可用/当前”流量值（字节）"""
    body_text = ""
    for _ in range(6):
        try:
            body_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            body_text = ""

        # 方案1：整页直接匹配“可用/剩余/当前流量 + 数值单位”
        direct_match = re.search(
            r"(?:可用|剩余|当前)\s*流量[^\d]{0,20}(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB))",
            body_text,
            re.IGNORECASE,
        )
        if direct_match:
            bytes_value = parse_traffic_to_bytes(direct_match.group(1))
            if bytes_value is not None:
                if logger:
                    logger.log_debug(f"直接匹配到剩余流量: {direct_match.group(0)}")
                return bytes_value

        # 方案2：按行兜底解析
        lines = [line.strip() for line in body_text.splitlines() if line.strip()]
        for line in lines:
            if "流量" in line and ("剩余" in line or "可用" in line or "当前" in line):
                bytes_value = parse_traffic_to_bytes(line)
                if bytes_value is not None:
                    if logger:
                        logger.log_debug(f"按行解析到剩余流量: {line}")
                    return bytes_value

        page.wait_for_timeout(1000)

    return None

def get_reward_traffic_bytes_from_page(page, logger=None):
    """尝试从页面文本中提取本次签到奖励流量"""
    try:
        body_text = page.locator("body").inner_text(timeout=2000)
    except Exception:
        return None

    reward_patterns = [
        r"(?:获得|奖励|增加|领取)[^\n]{0,30}?(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB))",
        r"(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB|KiB|MiB|GiB|TiB))[^\n]{0,20}?(?:流量)",
    ]

    for pattern in reward_patterns:
        match = re.search(pattern, body_text, re.IGNORECASE)
        if match:
            bytes_value = parse_traffic_to_bytes(match.group(1))
            if bytes_value is not None:
                if logger:
                    logger.log_debug(f"解析到奖励流量文本: {match.group(0)}")
                return bytes_value
    return None

def get_local_ip():
    """获取本机对外出口IP（局域网IP）"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            return ip
        finally:
            sock.close()
    except Exception:
        return "unknown"

def send_email_notification(subject, contents, logger=None):
    """发送邮件通知的通用函数"""
    enabled = os.getenv("EMAIL_NOTIFY_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        return False

    smtp_user = os.getenv("EMAIL_SMTP_USER", "").strip()
    smtp_password = os.getenv("EMAIL_SMTP_PASSWORD", "").strip()
    smtp_host = os.getenv("EMAIL_SMTP_HOST", "smtp.qq.com").strip() or "smtp.qq.com"
    to_raw = os.getenv("EMAIL_TO", smtp_user).strip()
    to_list = [x.strip() for x in to_raw.split(",") if x.strip()]

    if not smtp_user or not smtp_password or not to_list:
        msg = "邮件通知已启用，但 SMTP 配置不完整（EMAIL_SMTP_USER / EMAIL_SMTP_PASSWORD / EMAIL_TO）"
        print(f"[WARNING] {msg}")
        if logger:
            logger.log_error(msg)
        return False

    try:
        import yagmail
        yagmail.SMTP(
            user=smtp_user,
            password=smtp_password,
            host=smtp_host,
        ).send(
            to=to_list,
            subject=subject,
            contents=contents,
        )
        print(f"[INFO] 邮件通知已发送: {', '.join(to_list)}")
        if logger:
            logger.log_info(f"邮件通知已发送: {', '.join(to_list)}")
        return True
    except Exception as e:
        print(f"[ERROR] 邮件发送失败: {e}")
        if logger:
            logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        return False

def load_alert_state():
    """读取连续失败告警状态"""
    default_state = {"consecutive_failures": 0, "last_alerted_failures": 0}
    try:
        if ALERT_STATE_FILE.exists():
            data = json.loads(ALERT_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    "consecutive_failures": int(data.get("consecutive_failures", 0)),
                    "last_alerted_failures": int(data.get("last_alerted_failures", 0)),
                }
    except Exception:
        pass
    return default_state

def save_alert_state(state):
    """保存连续失败告警状态"""
    try:
        ALERT_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

def update_failure_alert_state(success, reason="", logger=None):
    """更新连续失败计数并在达到阈值时发送告警邮件"""
    alert_enabled = os.getenv("FAIL_ALERT_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    threshold_str = os.getenv("FAIL_ALERT_THRESHOLD", "2").strip()
    try:
        threshold = max(1, int(threshold_str))
    except Exception:
        threshold = 2

    state = load_alert_state()

    if success:
        if state.get("consecutive_failures", 0) > 0 and logger:
            logger.log_info(f"连续失败计数已清零（原值: {state.get('consecutive_failures', 0)}）")
        state["consecutive_failures"] = 0
        state["last_alerted_failures"] = 0
        save_alert_state(state)
        return

    state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
    current_failures = state["consecutive_failures"]
    save_alert_state(state)

    if logger:
        logger.log_error(f"本次运行失败，连续失败次数: {current_failures}")

    should_alert = (
        alert_enabled
        and current_failures >= threshold
        and current_failures > int(state.get("last_alerted_failures", 0))
    )

    if not should_alert:
        return

    subject = f"[告警] SakuraFRP 连续失败 {current_failures} 次 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    contents = (
        f"SakuraFRP 自动签到连续失败告警。\n\n"
        f"连续失败次数：{current_failures}\n"
        f"告警阈值：{threshold}\n"
        f"失败原因：{reason or '未知'}\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"主机：{os.uname().nodename if hasattr(os, 'uname') else 'unknown'}\n"
        f"IP：{get_local_ip()}\n"
        f"日志目录：{BASE_DIR / 'logs'}"
    )

    if send_email_notification(subject, contents, logger=logger):
        state["last_alerted_failures"] = current_failures
        save_alert_state(state)

def goto_with_retry(page, url, logger=None, attempts=3):
    """访问页面并在超时时自动重试，降低网络抖动影响"""
    # 先用 domcontentloaded，最后一次再尝试 load
    wait_sequence = ["domcontentloaded", "domcontentloaded", "load"]
    last_error = None

    for i in range(attempts):
        wait_until = wait_sequence[i] if i < len(wait_sequence) else "load"
        timeout_ms = 45000 if i < attempts - 1 else 60000
        try:
            if logger:
                logger.log_debug(f"页面访问尝试 {i + 1}/{attempts}, wait_until={wait_until}, timeout={timeout_ms}ms")
            page.goto(url, timeout=timeout_ms, wait_until=wait_until)
            return True
        except Exception as e:
            last_error = e
            print(f"[WARNING] 第 {i + 1}/{attempts} 次访问失败: {e}")
            if logger:
                logger.log_error(f"第 {i + 1}/{attempts} 次访问失败: {e}")
            if i < attempts - 1:
                time.sleep(2)

    if last_error:
        raise last_error
    return False

def capture_locator_screenshot(locator, logger=None, name="元素", attempts=3, timeout_ms=8000):
    """稳定截图：短超时 + 重试，避免偶发字体加载卡住导致整体失败"""
    last_error = None
    for i in range(attempts):
        try:
            return locator.screenshot(timeout=timeout_ms)
        except Exception as e:
            last_error = e
            if logger:
                logger.log_debug(f"{name}截图失败 {i + 1}/{attempts}: {e}")
            time.sleep(0.8)

    if last_error:
        raise last_error
    raise RuntimeError(f"{name}截图失败")

def send_success_email(
    logger=None,
    before_traffic_bytes=None,
    reward_traffic_bytes=None,
    after_traffic_bytes=None,
    account_name="unknown",
):
    """签到成功（含已签到）后发送邮件通知"""
    subject = f"SakuraFRP 签到成功通知 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    before_text = format_traffic(before_traffic_bytes)
    reward_text = format_traffic(reward_traffic_bytes)
    after_text = format_traffic(after_traffic_bytes)
    host_name = os.uname().nodename if hasattr(os, "uname") else "unknown"
    local_ip = get_local_ip()
    log_file_path = str(logger.log_file) if logger and getattr(logger, "log_file", None) else str(BASE_DIR / "logs")
    contents = (
        f"签到任务执行成功（包括“今日已签到”状态）。\n\n"
        f"账号：{account_name}\n"
        f"签到前剩余流量：{before_text}\n"
        f"本次签到获得流量：{reward_text}\n"
        f"签到后剩余流量：{after_text}\n\n"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"主机：{host_name}\n"
        f"IP：{local_ip}\n"
        f"日志文件：{log_file_path}"
    )

    send_email_notification(subject, contents, logger=logger)

def send_failure_email(reason, logger=None, account_name="unknown"):
    """最终失败后发送邮件通知"""
    subject = f"[失败] SakuraFRP 签到失败通知 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    host_name = os.uname().nodename if hasattr(os, "uname") else "unknown"
    local_ip = get_local_ip()
    log_file_path = str(logger.log_file) if logger and getattr(logger, "log_file", None) else str(BASE_DIR / "logs")

    current_attempt = os.getenv("CHECKIN_CURRENT_ATTEMPT", "")
    total_attempts = os.getenv("CHECKIN_TOTAL_ATTEMPTS", "")
    attempt_text = ""
    if current_attempt and total_attempts:
        attempt_text = f"重试轮次：{current_attempt}/{total_attempts}\n"

    contents = (
        f"SakuraFRP 自动签到最终失败。\n\n"
        f"账号：{account_name}\n"
        f"失败原因：{reason or '未知'}\n"
        f"{attempt_text}"
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"主机：{host_name}\n"
        f"IP：{local_ip}\n"
        f"日志文件：{log_file_path}"
    )

    send_email_notification(subject, contents, logger=logger)

# ---------------- 使用专业库识别缺口 ----------------
def identify_gap_with_library(bg_img_bytes, logger=None):
    """使用 captcha-recognizer 库识别滑块验证码缺口位置"""
    try:
        from captcha_recognizer.slider import Slider
        import numpy as np
        from PIL import Image
        
        # 将字节数据转换为 numpy 数组（库支持这种格式）
        bg_img = Image.open(io.BytesIO(bg_img_bytes))
        bg_arr = np.array(bg_img)
        
        # 使用 captcha-recognizer 库识别缺口
        # box 格式: [x1, y1, x2, y2] 对应缺口的左上角和右下角坐标
        # confidence: 置信度
        box, confidence = Slider().identify(source=bg_arr, show=False)
        
        if box and len(box) >= 4:
            x1, y1, x2, y2 = box
            gap_position = int(x1)  # 使用左上角的x坐标作为缺口位置
            print(f"[DEBUG] captcha-recognizer 识别结果: 缺口位置={gap_position}px, 置信度={confidence:.2f}")
            print(f"[DEBUG] 缺口完整坐标: 左上角({x1}, {y1}), 右下角({x2}, {y2})")
            
            if logger:
                logger.log_debug(f"captcha-recognizer: 缺口={gap_position}px, 置信度={confidence:.2f}")
            
            return gap_position
        else:
            print("[WARNING] captcha-recognizer 未识别到缺口")
            return 0
        
    except ImportError as e:
        print(f"[WARNING] captcha-recognizer 库未安装: {e}")
        print("[INFO] 请运行: pip install captcha-recognizer")
        return 0
    except Exception as e:
        print(f"[ERROR] captcha-recognizer 识别异常: {e}")
        import traceback
        traceback.print_exc()
        return 0

def identify_gap_local(bg_img_bytes):
    """备用方案：使用简单的边缘检测识别缺口位置"""
    try:
        import numpy as np
        from PIL import Image
        
        # 读取背景图
        bg_img = Image.open(io.BytesIO(bg_img_bytes))
        bg_arr = np.array(bg_img.convert('RGB'))
        
        # 转换为灰度图
        if len(bg_arr.shape) == 3:
            gray = np.mean(bg_arr, axis=2).astype(np.uint8)
        else:
            gray = bg_arr
        
        height, width = gray.shape
        
        # 计算每列的边缘强度
        edge_strength = np.zeros(width)
        for x in range(1, width - 1):
            gradient = np.abs(gray[:, x+1].astype(int) - gray[:, x-1].astype(int))
            edge_strength[x] = np.sum(gradient)
        
        # 找到边缘强度最大的位置
        margin = width // 10
        search_range = edge_strength[margin:width-margin]
        if len(search_range) > 0:
            max_idx = np.argmax(search_range) + margin
            print(f"[DEBUG] 简单边缘检测找到位置: {max_idx}px")
            return max_idx
        
        # 默认返回中间偏右位置
        return int(width * 0.6)
        
    except Exception as e:
        print(f"[ERROR] 简单边缘检测异常: {e}")
        return 0

# ---------------- 验证码类型检测 ----------------
def detect_captcha_type(page, logger=None):
    """检测验证码类型：九宫格或滑块"""
    # 先检查是否有验证码弹窗出现
    captcha_popup_visible = False
    try:
        # 检查多种可能的验证码弹窗选择器
        popup_selectors = [
            ".geetest_popup",
            ".geetest_wrap",
            ".geetest_panel",
            "[class*='geetest'][class*='popup']",
            "[class*='geetest'][class*='wrap']"
        ]
        for selector in popup_selectors:
            try:
                if page.locator(selector).is_visible(timeout=500):
                    captcha_popup_visible = True
                    print(f"[DEBUG] 检测到验证码弹窗: {selector}")
                    break
            except:
                continue
    except:
        pass
    
    if not captcha_popup_visible:
        # 不打印，避免日志过多
        pass
    
    # 检查九宫格验证码（增加超时时间）
    grid_visible = False
    grid_selectors = [
        ".geetest_table_box",
        ".geetest_grid",
        "[class*='table'][class*='box']"
    ]
    for selector in grid_selectors:
        try:
            if page.locator(selector).is_visible(timeout=2000):
                grid_visible = True
                print(f"[DEBUG] 检测到九宫格验证码元素: {selector}")
                break
        except:
            continue
    
    # 检查滑块验证码（增加超时时间和更多选择器）
    slider_visible = False
    slider_button_visible = False
    slider_selectors = [
        ".geetest_slider",
        ".geetest_slider_button",
        ".geetest_slider_track",
        ".geetest_canvas_bg",
        ".geetest_canvas_slice",
        "[class*='slider']",
        "[class*='canvas'][class*='bg']"
    ]
    
    for selector in slider_selectors:
        try:
            if page.locator(selector).is_visible(timeout=2000):
                if "button" in selector or "knob" in selector:
                    slider_button_visible = True
                    print(f"[DEBUG] 检测到滑块按钮: {selector}")
                elif "canvas" in selector or "bg" in selector:
                    slider_visible = True
                    print(f"[DEBUG] 检测到滑块canvas: {selector}")
                else:
                    slider_visible = True
                    print(f"[DEBUG] 检测到滑块元素: {selector}")
        except:
            continue
    
    # 打印所有geetest相关元素（用于调试）- 只在未检测到验证码时打印
    if not grid_visible and not slider_visible and not slider_button_visible:
        try:
            all_geetest = page.locator("[class*='geetest']").count()
            if all_geetest > 0:
                print(f"[DEBUG] 页面上共有 {all_geetest} 个包含'geetest'的元素")
                # 检查前几个元素的可见性
                visible_count = 0
                for i in range(min(10, all_geetest)):
                    try:
                        elem = page.locator("[class*='geetest']").nth(i)
                        class_name = elem.get_attribute("class") or ""
                        is_visible = elem.is_visible(timeout=500)
                        if is_visible:
                            visible_count += 1
                            print(f"[DEBUG]   可见元素 {visible_count}: class='{class_name[:80]}'")
                    except:
                        pass
                if visible_count == 0:
                    print(f"[DEBUG]   所有 {all_geetest} 个geetest元素都不可见")
        except Exception as e:
            print(f"[DEBUG] 检查geetest元素时出错: {e}")
    
    if grid_visible:
        print("[DEBUG] 检测到九宫格验证码")
        if logger:
            logger.log_debug("检测到九宫格验证码")
        return "grid"
    elif slider_visible or slider_button_visible:
        print("[DEBUG] 检测到滑块验证码")
        if logger:
            logger.log_debug("检测到滑块验证码")
        return "slider"
    else:
        print("[DEBUG] 未检测到已知的验证码类型")
        if logger:
            logger.log_debug("未检测到已知的验证码类型")
        return "unknown"

# ---------------- 验证码核心处理 ----------------
def solve_geetest_multistep(page, ai_service, logger=None):
    """使用AI服务处理九宫格验证码"""
    print("[INFO] 开始处理九宫格验证码...")
    if logger:
        logger.log_captcha_step("开始", "初始化验证码处理")
    
    img_container = page.locator(".geetest_table_box").first
    container_visible = False
    try:
        container_visible = img_container.is_visible(timeout=3000)
    except:
        pass
    
    if not container_visible:
        print("[DEBUG] 验证码容器不可见")
        if logger:
            logger.log_element_status("验证码容器", False)
        return False
    
    if logger:
        logger.log_element_status("验证码容器", True)
        
    # 步骤 1: 识别题目
    target_object = ""
    tip_img = page.locator(".geetest_tip_img").first
    tip_img_visible = False
    try:
        tip_img_visible = tip_img.is_visible(timeout=2000)
    except:
        pass
    
    if tip_img_visible:
        print("[DEBUG] 检测到图片提示，使用AI识别...")
        if logger:
            logger.log_captcha_step("步骤1", "检测到图片提示，使用AI识别")
        try:
            tip_img_bytes = capture_locator_screenshot(tip_img, logger=logger, name="题目提示图")
            target_object = ai_service.call_vision(tip_img_bytes, "图中是什么物体？只回答物体名称，不要带标点。")
            print(f"[DEBUG] AI识别结果（原始）: {target_object}")
        except Exception as e:
            print(f"[ERROR] AI识别图片提示失败: {e}")
            if logger:
                logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
    else:
        tip_text_loc = page.locator(".geetest_tip_content").first
        tip_text_visible = False
        try:
            tip_text_visible = tip_text_loc.is_visible(timeout=2000)
        except:
            pass
        
        if tip_text_visible:
            print("[DEBUG] 检测到文本提示，读取文本...")
            if logger:
                logger.log_captcha_step("步骤1", "检测到文本提示，读取文本")
            try:
                target_object = tip_text_loc.inner_text()
                print(f"[DEBUG] 文本提示内容: {target_object}")
            except Exception as e:
                print(f"[ERROR] 读取文本提示失败: {e}")
                if logger:
                    logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        else:
            print("[WARNING] 未找到题目提示（图片或文本）")
            if logger:
                logger.log_captcha_step("步骤1", "未找到题目提示")
    
    target_object = re.sub(r'[^\w]', '', target_object) # 过滤掉标点
    print(f">>> [Step 1] 识别题目为：【{target_object}】")
    if logger:
        logger.log_captcha_step("步骤1完成", f"识别题目: {target_object}")

    # 步骤 2-4: 逐行抠图识别
    print("[DEBUG] 开始逐行识别九宫格...")
    if logger:
        logger.log_captcha_step("步骤2-4", "开始逐行识别九宫格")
    
    all_descriptions = []
    try:
        # 获取整个九宫格的截图并在内存中处理
        grid_bytes = capture_locator_screenshot(img_container, logger=logger, name="九宫格")
        grid_img = Image.open(io.BytesIO(grid_bytes))
        w, h = grid_img.size
        row_h = h / 3
        print(f"[DEBUG] 九宫格尺寸: {w}x{h}, 每行高度: {row_h}")
        if logger:
            logger.log_captcha_step("步骤2-4", f"九宫格尺寸: {w}x{h}")
        
        for i in range(3):
            print(f"[DEBUG] 正在识别第 {i+1} 行...")
            if logger:
                logger.log_captcha_step(f"步骤{i+2}", f"识别第 {i+1} 行")
            
            # 裁剪出每一行
            top = i * row_h
            bottom = (i + 1) * row_h
            row_crop = grid_img.crop((0, top, w, bottom))
            
            buf = io.BytesIO()
            row_crop.save(buf, format='PNG')
            row_res = ai_service.identify_captcha_row(buf.getvalue(), i+1)
            print(f"[DEBUG] 第 {i+1} 行识别结果: {row_res}")
            if logger:
                logger.log_captcha_step(f"步骤{i+2}完成", f"第 {i+1} 行: {row_res}")
            all_descriptions.extend(row_res)
    except Exception as e:
        print(f"[ERROR] 九宫格识别过程出错: {e}")
        if logger:
            logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        return False

    # 步骤 5: 语义匹配并模拟点击
    print(f"[DEBUG] 开始语义匹配，目标: {target_object}, 描述列表: {all_descriptions}")
    if logger:
        logger.log_captcha_step("步骤5", f"语义匹配 - 目标: {target_object}")
    
    try:
        click_indices = ai_service.semantic_match(target_object, all_descriptions)
        print(f">>> [Final] 最终决定点击序号: {click_indices}")
        if logger:
            logger.log_captcha_step("步骤5完成", f"匹配结果: {click_indices}")
    except Exception as e:
        print(f"[ERROR] 语义匹配失败: {e}")
        if logger:
            logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        return False
    
    if not click_indices:
        print("[INFO] 未找到匹配项，刷新验证码...")
        if logger:
            logger.log_captcha_step("步骤5", "未找到匹配项，刷新验证码")
        try:
            refresh_btn = page.locator(".geetest_refresh").first
            if refresh_btn.is_visible():
                refresh_btn.click()
                time.sleep(2)
        except Exception as e:
            print(f"[ERROR] 刷新验证码失败: {e}")
            if logger:
                logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        return False

    try:
        box = img_container.bounding_box()
        cell_w, cell_h = box['width']/3, box['height']/3
        print(f"[DEBUG] 验证码容器位置: x={box['x']}, y={box['y']}, 宽度={box['width']}, 高度={box['height']}")
        print(f"[DEBUG] 每个格子尺寸: {cell_w}x{cell_h}")
        if logger:
            logger.log_captcha_step("点击", f"容器位置: ({box['x']}, {box['y']}), 格子尺寸: {cell_w}x{cell_h}")
        
        click_count = 0
        for idx in click_indices:
            try:
                val = int(idx)
                if 1 <= val <= 9:
                    r, c = (val-1)//3, (val-1)%3
                    # 点击格子的中心点
                    target_x = box['x'] + c*cell_w + cell_w/2
                    target_y = box['y'] + r*cell_h + cell_h/2
                    print(f"[DEBUG] 点击格子 {val} (行{r+1}, 列{c+1}), 坐标: ({target_x}, {target_y})")
                    if logger:
                        logger.log_captcha_step("点击", f"格子 {val} (行{r+1}, 列{c+1})")
                    page.mouse.click(target_x, target_y)
                    click_count += 1
                    time.sleep(random.uniform(0.3, 0.5))
            except Exception as e:
                print(f"[ERROR] 点击格子 {idx} 失败: {e}")
                if logger:
                    logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
                continue
        
        print(f"[DEBUG] 共点击了 {click_count} 个格子")
        if logger:
            logger.log_captcha_step("点击完成", f"共点击 {click_count} 个格子")
    except Exception as e:
        print(f"[ERROR] 获取验证码容器位置失败: {e}")
        if logger:
            logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        return False
            
    # 提交验证
    print("[DEBUG] 查找提交按钮...")
    if logger:
        logger.log_captcha_step("提交", "查找提交按钮")
    
    submit_success = False
    for sel in [".geetest_commit", "text=确认", ".geetest_submit"]:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=2000):
                print(f"[DEBUG] 找到提交按钮: {sel}")
                if logger:
                    logger.log_captcha_step("提交", f"找到按钮: {sel}")
                btn.click()
                submit_success = True
                break
        except:
            continue
    
    if not submit_success:
        print("[WARNING] 未找到提交按钮")
        if logger:
            logger.log_captcha_step("提交", "未找到提交按钮")
        return False
    
    print("[DEBUG] 验证码处理完成")
    if logger:
        logger.log_captcha_step("完成", "验证码处理完成")
    return True

def solve_geetest_slider(page, ai_service, logger=None):
    """使用AI服务处理滑块验证码"""
    print("[INFO] 开始处理滑块验证码...")
    if logger:
        logger.log_captcha_step("开始", "初始化滑块验证码处理")
    
    # 查找滑块相关元素
    slider_button = None
    slider_track = None
    
    # 尝试多种选择器找到滑块按钮
    slider_selectors = [
        ".geetest_slider_button",
        ".geetest_slider_knob",
        ".geetest_btn",
        "[class*='slider'][class*='button']"
    ]
    
    for selector in slider_selectors:
        try:
            btn = page.locator(selector).first
            if btn.is_visible(timeout=1000):
                slider_button = btn
                print(f"[DEBUG] 找到滑块按钮: {selector}")
                if logger:
                    logger.log_element_status("滑块按钮", True, f"选择器: {selector}")
                break
        except:
            continue
    
    if not slider_button:
        print("[ERROR] 未找到滑块按钮")
        if logger:
            logger.log_element_status("滑块按钮", False)
        return False
    
    # 获取滑块按钮的初始位置（用于计算偏移量）
    button_box = slider_button.bounding_box()
    if not button_box:
        print("[ERROR] 无法获取滑块按钮位置")
        if logger:
            logger.log_element_status("滑块按钮", False, "无法获取位置")
        return False
    
    button_initial_x = button_box['x']
    print(f"[DEBUG] 滑块按钮初始x坐标: {button_initial_x:.1f}")
    if logger:
        logger.log_debug(f"滑块按钮初始x坐标: {button_initial_x:.1f}")
    
    # 查找滑块轨道
    track_selectors = [
        ".geetest_slider_track",
        ".geetest_slider",
        "[class*='slider'][class*='track']"
    ]
    
    for selector in track_selectors:
        try:
            track = page.locator(selector).first
            if track.is_visible(timeout=1000):
                slider_track = track
                print(f"[DEBUG] 找到滑块轨道: {selector}")
                if logger:
                    logger.log_element_status("滑块轨道", True, f"选择器: {selector}")
                break
        except:
            continue
    
    # 获取验证码图片
    print("[DEBUG] 正在获取验证码图片...")
    if logger:
        logger.log_captcha_step("步骤1", "获取验证码图片")
    
    # 打印验证码相关的所有元素信息（用于调试）
    try:
        print("[DEBUG] 查找所有验证码相关元素...")
        all_geetest_elements = page.locator("[class*='geetest']").all()
        print(f"[DEBUG] 找到 {len(all_geetest_elements)} 个包含'geetest'的元素")
        for i, elem in enumerate(all_geetest_elements[:10]):  # 只打印前10个
            try:
                class_name = elem.get_attribute("class") or ""
                tag_name = elem.evaluate("el => el.tagName")
                is_visible = elem.is_visible(timeout=500)
                print(f"[DEBUG]   元素 {i+1}: <{tag_name}> class='{class_name}' visible={is_visible}")
            except:
                pass
    except Exception as e:
        print(f"[DEBUG] 获取元素信息失败（非关键）: {e}")
    
    # 尝试获取背景图和缺口图
    bg_img_bytes = None
    slice_img_bytes = None
    
    # 方法1: 从canvas获取
    canvas_selectors = [
        ".geetest_canvas_bg",
        ".geetest_canvas_slice",
        "canvas.geetest_canvas_bg",
        "canvas.geetest_canvas_slice"
    ]
    
    bg_canvas = None
    slice_canvas = None
    
    for selector in canvas_selectors:
        try:
            canvas = page.locator(selector).first
            if canvas.is_visible(timeout=1000):
                try:
                    box = canvas.bounding_box()
                    size_info = f"位置: ({box['x']:.0f}, {box['y']:.0f}), 尺寸: {box['width']:.0f}x{box['height']:.0f}" if box else "无法获取位置"
                    if "bg" in selector or "background" in selector.lower():
                        bg_canvas = canvas
                        print(f"[DEBUG] 找到背景canvas: {selector}, {size_info}")
                    elif "slice" in selector or "puzzle" in selector.lower():
                        slice_canvas = canvas
                        print(f"[DEBUG] 找到缺口canvas: {selector}, {size_info}")
                except:
                    if "bg" in selector or "background" in selector.lower():
                        bg_canvas = canvas
                        print(f"[DEBUG] 找到背景canvas: {selector}")
                    elif "slice" in selector or "puzzle" in selector.lower():
                        slice_canvas = canvas
                        print(f"[DEBUG] 找到缺口canvas: {selector}")
        except:
            continue
    
    # 如果找到canvas，截图
    if bg_canvas:
        try:
            bg_img_bytes = bg_canvas.screenshot()
            print("[DEBUG] 成功获取背景图")
            # 保存背景图到文件
            bg_img_path = BASE_DIR / "captcha_bg.png"
            with open(bg_img_path, "wb") as f:
                f.write(bg_img_bytes)
            print(f"[DEBUG] 背景图已保存到: {bg_img_path}")
            if logger:
                logger.log_captcha_step("步骤1", f"成功获取背景图，已保存到: {bg_img_path}")
        except Exception as e:
            print(f"[ERROR] 获取背景图失败: {e}")
            if logger:
                logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
    
    if slice_canvas:
        try:
            slice_img_bytes = slice_canvas.screenshot()
            print("[DEBUG] 成功获取缺口图")
            # 保存缺口图到文件
            slice_img_path = BASE_DIR / "captcha_slice.png"
            with open(slice_img_path, "wb") as f:
                f.write(slice_img_bytes)
            print(f"[DEBUG] 缺口图已保存到: {slice_img_path}")
            if logger:
                logger.log_captcha_step("步骤1", f"成功获取缺口图，已保存到: {slice_img_path}")
        except Exception as e:
            print(f"[ERROR] 获取缺口图失败: {e}")
            if logger:
                logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
    
    # 方法2: 如果canvas不可用，尝试从img标签获取
    if not bg_img_bytes:
        img_selectors = [
            ".geetest_bg img",
            ".geetest_slice_bg img",
            "[class*='bg'] img"
        ]
        for selector in img_selectors:
            try:
                img = page.locator(selector).first
                if img.is_visible(timeout=1000):
                    bg_img_bytes = img.screenshot()
                    print(f"[DEBUG] 从img标签获取背景图: {selector}")
                    # 保存从img标签获取的背景图
                    bg_img_path = BASE_DIR / "captcha_bg.png"
                    with open(bg_img_path, "wb") as f:
                        f.write(bg_img_bytes)
                    print(f"[DEBUG] 背景图已保存到: {bg_img_path}")
                    break
            except:
                continue
    
    if not bg_img_bytes:
        print("[WARNING] 无法获取验证码图片，尝试截图整个验证码区域")
        if logger:
            logger.log_captcha_step("步骤1", "无法获取图片，尝试截图整个区域")
        try:
            # 尝试截图整个验证码容器
            captcha_container = page.locator(".geetest_popup, .geetest_wrap, [class*='geetest']").first
            if captcha_container.is_visible(timeout=2000):
                bg_img_bytes = captcha_container.screenshot()
                print("[DEBUG] 成功截图验证码容器")
                # 保存整个验证码区域截图
                container_img_path = BASE_DIR / "captcha_container.png"
                with open(container_img_path, "wb") as f:
                    f.write(bg_img_bytes)
                print(f"[DEBUG] 验证码容器截图已保存到: {container_img_path}")
                if logger:
                    logger.log_captcha_step("步骤1", f"验证码容器截图已保存到: {container_img_path}")
        except Exception as e:
            print(f"[ERROR] 截图验证码容器失败: {e}")
            if logger:
                logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
            return False
    
    # 查找缺口块元素（拼图块）
    slice_element = None
    slice_element_selectors = [
        ".geetest_slice",
        ".geetest_slice_box",
        "[class*='slice']",
        "[class*='puzzle']"
    ]
    
    for selector in slice_element_selectors:
        try:
            elem = page.locator(selector).first
            if elem.is_visible(timeout=1000):
                slice_element = elem
                try:
                    box = elem.bounding_box()
                    if box:
                        print(f"[DEBUG] 找到缺口块元素: {selector}, 位置: ({box['x']:.0f}, {box['y']:.0f}), 尺寸: {box['width']:.0f}x{box['height']:.0f}")
                        if logger:
                            logger.log_element_status("缺口块元素", True, f"位置: ({box['x']:.0f}, {box['y']:.0f})")
                except:
                    print(f"[DEBUG] 找到缺口块元素: {selector}")
                break
        except:
            continue
    
    # 额外保存整个页面的验证码区域截图（用于调试）
    try:
        full_captcha_path = BASE_DIR / "captcha_full.png"
        # 尝试找到验证码弹窗并截图
        captcha_popup = page.locator(".geetest_popup, .geetest_wrap").first
        if captcha_popup.is_visible(timeout=1000):
            full_captcha_bytes = captcha_popup.screenshot()
            with open(full_captcha_path, "wb") as f:
                f.write(full_captcha_bytes)
            print(f"[DEBUG] 完整验证码区域已保存到: {full_captcha_path}")
            if logger:
                logger.log_debug(f"完整验证码区域已保存到: {full_captcha_path}")
    except Exception as e:
        print(f"[DEBUG] 保存完整验证码区域失败（非关键错误）: {e}")
    
    # 使用 captcha-recognizer 专业库识别缺口位置
    print("[DEBUG] 使用 captcha-recognizer 库识别缺口位置...")
    if logger:
        logger.log_captcha_step("步骤2", "使用 captcha-recognizer 库识别缺口")
    
    gap_position = 0
    
    try:
        gap_position = identify_gap_with_library(bg_img_bytes, logger)
        if gap_position > 0:
            print(f"[INFO] captcha-recognizer 识别成功: 缺口位置={gap_position}px")
            if logger:
                logger.log_captcha_step("步骤2完成", f"识别成功: {gap_position}px")
        else:
            print("[ERROR] captcha-recognizer 未识别到缺口")
            if logger:
                logger.log_captcha_step("步骤2", "未识别到缺口")
            return False
    except Exception as e:
        error_msg = f"captcha-recognizer 识别失败: {e}"
        print(f"[ERROR] {error_msg}")
        if logger:
            logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        return False
    
    if gap_position <= 0:
        print("[ERROR] 识别到的缺口位置无效")
        if logger:
            logger.log_captcha_step("步骤2", "识别到的缺口位置无效")
        return False
    
    # 获取背景canvas的位置（用于计算偏移量和缺口实际位置）
    bg_canvas_box = None
    if bg_canvas:
        try:
            bg_canvas_box = bg_canvas.bounding_box()
            if bg_canvas_box:
                print(f"[DEBUG] 背景canvas位置: x={bg_canvas_box['x']:.1f}, y={bg_canvas_box['y']:.1f}, 尺寸={bg_canvas_box['width']:.1f}x{bg_canvas_box['height']:.1f}")
                if logger:
                    logger.log_debug(f"背景canvas: x={bg_canvas_box['x']:.1f}, 宽={bg_canvas_box['width']:.1f}")
        except Exception as e:
            print(f"[DEBUG] 获取背景canvas位置失败: {e}")
    
    # 计算坐标转换
    # gap_position 是缺口在背景图中的x坐标（图片坐标系，相对于图片左边缘）
    # 需要转换为页面坐标系，然后计算滑动距离
    
    button_x = button_box['x'] + button_box['width'] / 2
    button_y = button_box['y'] + button_box['height'] / 2
    button_width = button_box['width']
    
    print(f"[DEBUG] 滑块按钮: 左边缘x={button_box['x']:.1f}, 中心x={button_x:.1f}, 宽度={button_width:.1f}")
    if logger:
        logger.log_captcha_step("步骤3", f"滑块按钮中心: ({button_x:.1f}, {button_y:.1f})")
    
    # 计算滑动距离
    gap_x_in_page = None  # 初始化变量
    offset = None  # 初始化变量
    
    if bg_canvas_box:
        # 使用背景canvas位置进行坐标转换
        bg_canvas_x = bg_canvas_box['x']
        
        # 计算滑块初始位置相对于背景图的偏移量
        # offset = 滑块按钮左边缘x - 背景canvas的x
        offset = button_initial_x - bg_canvas_x
        print(f"[DEBUG] 计算偏移量: 滑块初始x({button_initial_x:.1f}) - 背景canvas x({bg_canvas_x:.1f}) = {offset:.1f}px")
        
        # 实际滑动距离 = 缺口位置 + 偏移量
        drag_distance_base = gap_position + offset
        print(f"[DEBUG] 基础滑动距离: 缺口位置({gap_position}px) + 偏移量({offset:.1f}px) = {drag_distance_base:.1f}px")
        
        # 添加随机误差，模拟人类操作（-5.0 到 +5.0 像素）
        human_error = random.uniform(-5.0, 5.0)
        drag_distance = drag_distance_base + human_error
        print(f"[DEBUG] 添加人类误差: {drag_distance_base:.1f}px + {human_error:.2f}px = {drag_distance:.1f}px")
        
        # 计算缺口在页面中的位置（用于显示）
        gap_x_in_page = bg_canvas_x + gap_position
        
        # 目标位置 = 当前按钮中心 + 滑动距离
        target_x = button_x + drag_distance
        
        if logger:
            logger.log_captcha_step("步骤3", f"偏移量={offset:.1f}, 滑动距离={drag_distance:.1f} (含误差{human_error:.2f}), 目标={target_x:.1f}")
    else:
        # 方法2: 如果没有背景canvas信息，直接使用gap_position作为滑动距离
        drag_distance_base = gap_position
        
        # 添加随机误差，模拟人类操作（-5.0 到 +5.0 像素）
        human_error = random.uniform(-5.0, 5.0)
        drag_distance = drag_distance_base + human_error
        
        target_x = button_x + drag_distance
        print(f"[DEBUG] 无背景canvas信息，直接滑动: {drag_distance_base}px + 误差{human_error:.2f}px = {drag_distance:.1f}px，目标位置: {target_x:.1f}")
        if logger:
            logger.log_captcha_step("步骤3", f"直接滑动距离={drag_distance:.1f}px (含误差{human_error:.2f})")
    
    # ===== 详细打印滑块起点和终点信息 =====
    print("\n" + "="*60)
    print(f"[INFO] 滑块拖动预测信息:")
    print(f"  滑块按钮左边缘X: {button_initial_x:.1f}px (页面坐标)")
    print(f"  滑块按钮中心X: {button_x:.1f}px (页面坐标)")
    if bg_canvas_box and offset is not None:
        print(f"  背景canvas X: {bg_canvas_box['x']:.1f}px (页面坐标)")
        print(f"  滑块初始偏移: {offset:.1f}px")
    print(f"  缺口位置: {gap_position}px (图片坐标)")
    if gap_x_in_page is not None:
        print(f"  缺口实际位置: {gap_x_in_page:.1f}px (页面坐标)")
    print(f"  滑动距离: {drag_distance:.1f}px (含±5px人类误差)")
    print(f"  目标中心X: {target_x:.1f}px (页面坐标)")
    print("="*60 + "\n")
    
    if logger:
        logger.log_captcha_step("步骤3完成", f"起点={button_x:.1f}, 终点={target_x:.1f}, 距离={drag_distance:.1f}")
    
    # ===== 拖动前截图 =====
    try:
        before_drag_path = BASE_DIR / "slider_before_drag.png"
        page.screenshot(path=str(before_drag_path))
        print(f"[INFO] 拖动前截图已保存: {before_drag_path}")
        if logger:
            logger.log_debug(f"拖动前截图已保存: {before_drag_path}")
    except Exception as e:
        print(f"[WARNING] 保存拖动前截图失败: {e}")
    
    # 执行拖动
    try:
        print(f"[DEBUG] 开始拖动滑块...")
        if logger:
            logger.log_captcha_step("步骤4", f"拖动: {button_x:.1f} -> {target_x:.1f}")
        
        # 先移动到按钮位置
        page.mouse.move(button_x, button_y)
        time.sleep(random.uniform(0.1, 0.2))
        
        # 按下鼠标
        page.mouse.down()
        time.sleep(random.uniform(0.1, 0.2))
        
        # 模拟人类拖动轨迹（使用 pytweening 缓动函数）
        steps = random.randint(20, 30)  # 增加步数，轨迹更平滑
        
        # 随机选择一个缓动函数，模拟不同人的操作习惯
        easing_functions = [
            pytweening.easeInOutQuad,    # 先加速后减速（最常见）
            pytweening.easeOutQuad,      # 快速启动，逐渐减速
            pytweening.easeInOutCubic,   # 更平滑的加速减速
        ]
        easing_func = random.choice(easing_functions)
        
        print(f"[DEBUG] 使用缓动函数: {easing_func.__name__}, 步数: {steps}")
        
        for i in range(steps):
            # 使用 pytweening 的缓动函数计算进度
            progress = easing_func(i / steps)
            
            # 添加随机抖动（水平和垂直）
            jitter_x = random.uniform(-1.5, 1.5)
            jitter_y = random.uniform(-2, 2)
            
            current_x = button_x + drag_distance * progress + jitter_x
            current_y = button_y + jitter_y
            
            page.mouse.move(current_x, current_y)
            
            # 根据速度调整时间间隔（移动快的时候间隔短，移动慢的时候间隔长）
            if i < steps * 0.3:  # 前30%，快速移动
                time.sleep(random.uniform(0.005, 0.015))
            elif i > steps * 0.7:  # 后30%，减速
                time.sleep(random.uniform(0.02, 0.04))
            else:  # 中间阶段
                time.sleep(random.uniform(0.01, 0.025))
        
        # 添加轻微的超调和回调（模拟人类操作的不精确性）
        if random.random() > 0.5:  # 50% 概率出现超调
            overshoot = random.uniform(2, 5)  # 超调2-5像素
            page.mouse.move(target_x + overshoot, button_y + random.uniform(-1, 1))
            time.sleep(random.uniform(0.05, 0.1))
            print(f"[DEBUG] 模拟超调: +{overshoot:.1f}px")
        
        # 最后精确移动到目标位置
        page.mouse.move(target_x, button_y)
        time.sleep(random.uniform(0.15, 0.25))
        
        # 释放鼠标
        page.mouse.up()
        time.sleep(random.uniform(0.5, 1.0))
        
        print("[DEBUG] 滑块拖动完成")
        if logger:
            logger.log_captcha_step("步骤4完成", "滑块拖动完成")
        
        # ===== 拖动后截图 =====
        try:
            after_drag_path = BASE_DIR / "slider_after_drag.png"
            page.screenshot(path=str(after_drag_path))
            print(f"[INFO] 拖动后截图已保存: {after_drag_path}")
            if logger:
                logger.log_debug(f"拖动后截图已保存: {after_drag_path}")
        except Exception as e:
            print(f"[WARNING] 保存拖动后截图失败: {e}")
        
        # 等待验证结果
        time.sleep(2)
        
        # 检查是否验证成功（验证码消失或出现成功提示）
        captcha_gone = True
        try:
            # 检查验证码是否还存在
            if page.locator(".geetest_slider").is_visible(timeout=1000):
                captcha_gone = False
        except:
            pass
        
        if captcha_gone:
            print("[DEBUG] 验证码已消失，可能验证成功")
            if logger:
                logger.log_captcha_step("完成", "验证码已消失")
            return True
        else:
            print("[DEBUG] 验证码仍存在，可能验证失败")
            if logger:
                logger.log_captcha_step("完成", "验证码仍存在，可能失败")
            return False
        
    except Exception as e:
        print(f"[ERROR] 拖动滑块失败: {e}")
        if logger:
            logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        return False

# ---------------- 主逻辑 ----------------
def find_signed_text_locator(page, timeout=3000):
    try:
        loc = page.get_by_text(ALREADY_SIGNED_TEXT).first
        if loc.is_visible(timeout=timeout):
            # 防止误判：如果“点击这里签到”按钮可见，则应视为未签到
            try:
                sign_btn = page.get_by_text("点击这里签到").first
                if sign_btn.is_visible(timeout=500):
                    return None
            except:
                pass
            return loc
    except: 
        pass
    return None

def is_sign_button_visible(page, timeout=1000):
    """检查签到按钮是否可见"""
    try:
        return page.get_by_text("点击这里签到").first.is_visible(timeout=timeout)
    except Exception:
        return False

def is_checkin_completed(page, logger=None):
    """更稳妥的签到完成判定"""
    current_url = ""
    try:
        current_url = page.url
    except Exception:
        current_url = ""

    # 登录页或非用户页一律视为未完成
    if "login" in current_url or "/user" not in current_url:
        if logger:
            logger.log_debug(f"签到完成复核 - 非用户页或登录页: {current_url}")
        return False

    # 1) 明确文案命中
    if find_signed_text_locator(page, timeout=800):
        return True

    # 2) 成功关键词（例如签到成功提示/奖励提示）
    try:
        body_text = page.locator("body").inner_text(timeout=1000)
    except Exception:
        body_text = ""

    # 必须是明确成功关键词，避免公告等文本误触发
    success_keywords = ["签到成功", "今天已经签到过啦"]
    has_success_kw = any(k in body_text for k in success_keywords)

    # 奖励文案需带流量单位才视为成功
    reward_match = re.search(r"获得\s*\d+(?:\.\d+)?\s*(?:GiB|GB|MiB|MB|TiB|TB)", body_text, re.IGNORECASE)
    has_reward_kw = reward_match is not None

    # 3) 签到按钮不可见 且 验证码弹层已消失
    sign_button_visible = is_sign_button_visible(page, timeout=500)
    captcha_visible = False
    try:
        captcha_visible = page.locator(".geetest_table_box").first.is_visible(timeout=500)
    except Exception:
        captcha_visible = False

    if logger:
        logger.log_debug(
            f"签到完成复核 - has_success_kw={has_success_kw}, has_reward_kw={has_reward_kw}, "
            f"sign_button_visible={sign_button_visible}, captcha_visible={captcha_visible}"
        )

    return has_success_kw or has_reward_kw

def dismiss_adult_popup(page, logger=None):
    """关闭18岁确认弹窗（若存在）"""
    try:
        btn_18 = page.get_by_text("是，我已满18岁")
        if btn_18.is_visible(timeout=1500):
            btn_18.click()
            page.wait_for_timeout(600)
            if logger:
                logger.log_debug("18岁弹窗已关闭")
            return True
    except Exception:
        pass
    return False

def main():
    run_success = False
    failure_reason = "未知错误"

    # 解析命令行参数
    parser = argparse.ArgumentParser(description='SakuraFRP自动签到脚本')
    parser.add_argument('--screenshot-only', action='store_true', help='仅记录截图，不记录日志')
    parser.add_argument('--log-only', action='store_true', help='仅记录日志，不保存截图')
    parser.add_argument('--both', action='store_true', help='同时记录截图和日志（默认）')
    args = parser.parse_args()
    
    # 确定记录模式
    if args.screenshot_only:
        save_screenshot = True
        save_log = False
    elif args.log_only:
        save_screenshot = False
        save_log = True
    else:
        # 默认或--both都是两者都要
        save_screenshot = True
        save_log = True
    
    # 清理30天前的旧日志
    clean_old_logs(BASE_DIR, days=30)
    
    # 初始化日志记录器（如果需要）
    logger = None
    if save_log:
        logger = CheckinLogger(BASE_DIR)
        logger.log_start()
    
    # 初始化AI服务
    try:
        ai_service = AIService()
    except Exception as e:
        error_msg = f"AI服务初始化失败: {e}"
        print(f"[ERROR] {error_msg}")
        if logger:
            logger.log_error(error_msg)
        update_failure_alert_state(False, error_msg, logger)
        return 1
    
    # 加载账号信息
    try:
        username, password = load_username_password(ACCOUNT_FILE)
    except Exception as e:
        error_msg = f"加载账号信息失败: {e}"
        print(f"[ERROR] {error_msg}")
        if logger:
            logger.log_error(error_msg)
        update_failure_alert_state(False, error_msg, logger)
        return 1

    with sync_playwright() as p:
        # 默认不复用 state.json，避免历史会话导致账号或签到状态错读
        use_state_cache = os.getenv("USE_STATE_CACHE", "false").strip().lower() in ("1", "true", "yes", "on")

        # 从环境变量读取代理配置（可选）
        proxy_url = os.getenv("HTTP_PROXY") or os.getenv("http_proxy")
        
        if proxy_url:
            print(f"[INFO] 使用代理: {proxy_url}")
            if logger:
                logger.log_info(f"使用代理: {proxy_url}")
            browser = p.chromium.launch(
                headless=True, 
                slow_mo=100,
                proxy={"server": proxy_url}
            )
        else:
            browser = p.chromium.launch(headless=True, slow_mo=100)
        context = browser.new_context(storage_state=STATE_FILE if (use_state_cache and STATE_FILE.exists()) else None)

        # 屏蔽字体资源，减少 screenshot 等待字体加载导致的超时
        def _route_handler(route):
            try:
                if route.request.resource_type == "font":
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass

        context.route("**/*", _route_handler)

        page = context.new_page()
        page.set_viewport_size({"width": 1280, "height": 900})
        
        print(f"[INFO] 正在访问: {target_url}")
        if logger:
            logger.log_info(f"正在访问: {target_url}")
        
        try:
            goto_with_retry(page, target_url, logger=logger, attempts=3)
            current_url = page.url
            print(f"[DEBUG] 页面加载完成，当前URL: {current_url}")
            if logger:
                logger.log_page_url(current_url)
        except Exception as e:
            error_msg = f"页面访问失败: {e}"
            print(f"[ERROR] {error_msg}")
            if logger:
                logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
            update_failure_alert_state(False, error_msg, logger)
            browser.close()
            return 1

        # 登录判断
        current_url_after_load = page.url
        print(f"[DEBUG] 登录检查前URL: {current_url_after_load}")
        if logger:
            logger.log_page_url(current_url_after_load)
        
        is_logged_in = True
        username_input_visible = False
        try:
            username_input_visible = page.locator("#username").is_visible(timeout=2000)
        except:
            pass
        
        if "login" in current_url_after_load or username_input_visible:
            is_logged_in = False
            print("[INFO] 检测到需要登录")
            print(f"[DEBUG] URL包含'login': {'login' in current_url_after_load}, 用户名输入框可见: {username_input_visible}")
            if logger:
                logger.log_login_status(False)
                logger.log_element_status("用户名输入框", username_input_visible, f"URL包含login: {'login' in current_url_after_load}")
            
            try:
                print("[INFO] 正在填写登录信息...")
                page.fill("#username", username)
                page.fill("#password", password)
                print("[INFO] 正在点击登录按钮...")
                page.click("#login")
                
                print("[DEBUG] 等待登录完成，检查'账号信息'文本...")
                if logger:
                    logger.log_debug("等待登录完成，检查'账号信息'文本...")
                
                try:
                    page.wait_for_selector("text=账号信息", timeout=10000)
                    if use_state_cache:
                        context.storage_state(path=STATE_FILE)
                    is_logged_in = True
                    print("[SUCCESS] 登录成功")
                    if logger:
                        logger.log_login_status(True)
                        logger.log_page_url(page.url)
                except Exception as e:
                    error_msg = f"登录超时或失败: {e}"
                    print(f"[ERROR] {error_msg}")
                    print(f"[DEBUG] 登录后URL: {page.url}")
                    if logger:
                        logger.log_error(error_msg)
                        logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
                        logger.log_page_url(page.url)
            except Exception as e:
                error_msg = f"登录过程出错: {e}"
                print(f"[ERROR] {error_msg}")
                if logger:
                    logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
        else:
            print("[INFO] 已登录状态")
            if logger:
                logger.log_login_status(True)

        # 18岁弹窗
        try:
            if dismiss_adult_popup(page, logger):
                print("[DEBUG] 检测到18岁确认弹窗，正在点击...")
        except Exception as e:
            if logger:
                logger.log_debug(f"18岁弹窗处理: {e}")
            pass

        # 登录状态二次确认（防止页面延迟跳转到登录页）
        try:
            current_url_recheck = page.url
        except Exception:
            current_url_recheck = ""

        username_input_visible_recheck = False
        try:
            username_input_visible_recheck = page.locator("#username").is_visible(timeout=1500)
        except Exception:
            username_input_visible_recheck = False

        if "login" in current_url_recheck or username_input_visible_recheck:
            print("[WARNING] 检测到会话跳转至登录页，执行二次登录...")
            if logger:
                logger.log_info("检测到会话跳转至登录页，执行二次登录")

            try:
                page.fill("#username", username)
                page.fill("#password", password)
                page.click("#login")
                page.wait_for_selector("text=账号信息", timeout=15000)
                if use_state_cache:
                    context.storage_state(path=STATE_FILE)
                print("[SUCCESS] 二次登录成功")
                if logger:
                    logger.log_info("二次登录成功")
            except Exception as e:
                error_msg = f"二次登录失败: {e}"
                print(f"[ERROR] {error_msg}")
                if logger:
                    logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
                update_failure_alert_state(False, error_msg, logger)
                browser.close()
                return 1

        # 签到
        before_traffic_bytes = get_remaining_traffic_bytes(page, logger)
        if before_traffic_bytes is not None:
            print(f"[INFO] 签到前剩余流量: {format_traffic(before_traffic_bytes)}")
            if logger:
                logger.log_info(f"签到前剩余流量: {format_traffic(before_traffic_bytes)}")

        reward_traffic_bytes = None
        after_traffic_bytes = None
        was_already_signed = False
        pending_failure_reason = None

        print("[DEBUG] 开始检查签到状态...")
        if logger:
            logger.log_debug("开始检查签到状态...")
        
        sign_success = False
        signed_locator = find_signed_text_locator(page)
        if signed_locator:
            was_already_signed = True
            print("[INFO] 今日已签到。")
            if logger:
                logger.log_already_signed()
        else:
            print("[DEBUG] 未检测到已签到状态，查找签到按钮...")
            if logger:
                logger.log_debug("未检测到已签到状态，查找签到按钮...")
            
            sign_btn = page.get_by_text("点击这里签到")
            sign_btn_visible = False
            try:
                sign_btn_visible = sign_btn.is_visible(timeout=3000)
            except:
                pass
            
            print(f"[DEBUG] 签到按钮可见性: {sign_btn_visible}")
            if logger:
                logger.log_element_status("签到按钮", sign_btn_visible)
            
            if sign_btn_visible:
                print("[INFO] 点击签到按钮...")
                if logger:
                    logger.log_info("点击签到按钮...")
                
                # 初始化签到成功标志
                sign_success = False
                captcha_appeared = False
                
                try:
                    # 二次登录后弹层可能重新出现，点击前再清理一次
                    dismiss_adult_popup(page, logger)
                    sign_btn.click()
                    print("[DEBUG] 已点击签到按钮，等待验证码加载...")
                    if logger:
                        logger.log_debug("已点击签到按钮，等待验证码加载")
                    
                    # 保存点击前的页面状态（用于对比）
                    try:
                        before_screenshot = BASE_DIR / "before_click.png"
                        page.screenshot(path=str(before_screenshot))
                        print(f"[DEBUG] 点击前页面截图已保存: {before_screenshot}")
                    except:
                        pass
                    
                    # 获取点击后的URL
                    current_url = page.url
                    print(f"[DEBUG] 点击后当前URL: {current_url}")
                    
                    # 第一次等待：等待15秒后进行第一次检查
                    print("[INFO] 等待15秒让验证码完全加载...")
                    for i in range(30):  # 30次，每次0.5秒，总共15秒
                        time.sleep(0.5)
                        
                        # 每5秒打印一次进度
                        if (i + 1) % 10 == 0:
                            print(f"[DEBUG] 已等待 {(i+1)*0.5:.1f} 秒...")
                        
                        # 检查是否已经签到成功（不需要验证码）
                        signed_check = find_signed_text_locator(page, timeout=500)
                        if signed_check:
                            print(f"[SUCCESS] 签到完成（无需验证码，等待了 {(i+1)*0.5:.1f} 秒）！")
                            sign_success = True
                            reward_traffic_bytes = get_reward_traffic_bytes_from_page(page, logger)
                            if logger:
                                logger.log_sign_success()
                            break
                    
                    if not sign_success:
                        # 第一次检查验证码
                        print("[DEBUG] 15秒等待结束，开始第一次检查验证码...")
                        captcha_type_check = detect_captcha_type(page, logger)
                        if captcha_type_check != "unknown":
                            captcha_appeared = True
                            print(f"[INFO] 验证码已出现（类型: {captcha_type_check}）")
                        else:
                            # 继续等待，每5秒检查一次，最多再等15秒（总共30秒）
                            print("[DEBUG] 未检测到验证码，继续等待...")
                            for check_round in range(3):  # 3轮，每轮5秒
                                print(f"[DEBUG] 等待第 {check_round + 1} 轮（5秒）...")
                                time.sleep(5)
                                
                                # 检查是否已经签到成功
                                signed_check = find_signed_text_locator(page, timeout=500)
                                if signed_check:
                                    print(f"[SUCCESS] 签到完成（无需验证码）！")
                                    sign_success = True
                                    reward_traffic_bytes = get_reward_traffic_bytes_from_page(page, logger)
                                    if logger:
                                        logger.log_sign_success()
                                    break
                                
                                # 检查验证码
                                captcha_type_check = detect_captcha_type(page, logger)
                                if captcha_type_check != "unknown":
                                    captcha_appeared = True
                                    print(f"[INFO] 验证码已出现（类型: {captcha_type_check}，总等待时间: {15 + (check_round + 1) * 5} 秒）")
                                    break
                                else:
                                    print(f"[DEBUG] 第 {check_round + 1} 轮检查：仍未检测到验证码")
                    
                    # 保存点击后的页面状态
                    try:
                        after_screenshot = BASE_DIR / "after_click.png"
                        page.screenshot(path=str(after_screenshot))
                        print(f"[DEBUG] 点击后页面截图已保存: {after_screenshot}")
                    except:
                        pass
                    
                    if sign_success:
                        # 如果已经签到成功，不需要继续处理验证码
                        pass
                    elif not captcha_appeared and not sign_success:
                        print("[WARNING] 点击签到按钮后30秒内未检测到验证码")
                        if logger:
                            logger.log_debug("点击签到按钮后30秒内未检测到验证码")
                    
                except Exception as e:
                    error_msg = f"点击签到按钮失败: {e}"
                    print(f"[ERROR] {error_msg}")
                    # 若被18岁弹层拦截，尝试关闭弹层并重试一次点击
                    retried = False
                    try:
                        if "adult-check" in str(e) or "intercepts pointer events" in str(e):
                            dismiss_adult_popup(page, logger)
                            sign_btn.click(timeout=8000)
                            retried = True
                            print("[INFO] 关闭18岁弹层后重试点击签到成功")
                    except Exception:
                        retried = False

                    if retried:
                        try:
                            time.sleep(1)
                            captcha_type_check = detect_captcha_type(page, logger)
                            captcha_appeared = captcha_type_check != "unknown"
                        except Exception:
                            pass

                    if logger:
                        logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
                
                # 如果已经签到成功，跳过验证码处理
                if not sign_success:
                    # 如果检测到验证码，进入处理流程；否则再尝试检查
                    if captcha_appeared:
                        base_attempts = 3
                        print("[DEBUG] 验证码已出现，开始处理...")
                    else:
                        # 30秒后仍未检测到验证码，再给最后2次机会（每次2秒）
                        base_attempts = 2
                        print("[DEBUG] 验证码未出现，再尝试检测2次...")

                    # 额外重试轮（默认开启，仅在验证码场景生效）
                    extra_attempts = 0
                    if captcha_appeared:
                        extra_enabled = os.getenv("CAPTCHA_EXTRA_ROUND_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
                        if extra_enabled:
                            try:
                                extra_attempts = max(0, int(os.getenv("CAPTCHA_EXTRA_ROUND_ATTEMPTS", "2").strip()))
                            except Exception:
                                extra_attempts = 2

                    max_attempts = base_attempts + extra_attempts

                    print(f"[DEBUG] 开始签到循环检测，最多尝试 {max_attempts} 次...")
                    if logger:
                        logger.log_debug(f"开始签到循环检测，最多尝试 {max_attempts} 次...")
                        if extra_attempts > 0:
                            logger.log_debug(f"已启用验证码额外重试轮: +{extra_attempts} 次")
                    
                    for attempt in range(1, max_attempts + 1):
                        if extra_attempts > 0 and attempt == base_attempts + 1:
                            print(f"[DEBUG] 进入额外重试轮（共 {extra_attempts} 次）...")
                            if logger:
                                logger.log_debug(f"进入额外重试轮（共 {extra_attempts} 次）")

                        print(f"[DEBUG] 第 {attempt}/{max_attempts} 次检查...")
                        if logger:
                            logger.log_debug(f"第 {attempt}/{max_attempts} 次检查...")
                        
                        # 检查是否已签到成功
                        signed_check = find_signed_text_locator(page, timeout=1000)
                        if signed_check or is_checkin_completed(page, logger):
                            print("[SUCCESS] 签到完成！")
                            sign_success = True
                            reward_traffic_bytes = get_reward_traffic_bytes_from_page(page, logger)
                            if logger:
                                logger.log_sign_success()
                                logger.log_debug(f"在第 {attempt} 次检查时检测到签到成功")
                            break
                        
                        # 检查是否有验证码（增加等待时间）
                        print(f"[DEBUG] 第 {attempt} 次检查：检测验证码类型...")
                        captcha_type = detect_captcha_type(page, logger)
                        
                        if captcha_type != "unknown":
                            print(f"[DEBUG] 第 {attempt} 次检查：检测到{('九宫格' if captcha_type == 'grid' else '滑块')}验证码")
                            if logger:
                                logger.log_captcha_step(f"第 {attempt} 次", f"检测到{('九宫格' if captcha_type == 'grid' else '滑块')}验证码")
                            
                            try:
                                if captcha_type == "grid":
                                    captcha_result = solve_geetest_multistep(page, ai_service, logger)
                                elif captcha_type == "slider":
                                    captcha_result = solve_geetest_slider(page, ai_service, logger)
                                else:
                                    captcha_result = False
                                
                                result_text = "成功" if captcha_result else "失败"
                                print(f"[DEBUG] 验证码处理结果: {result_text}")
                                if logger:
                                    logger.log_captcha_result(result_text)
                                    logger.log_captcha_step(f"第 {attempt} 次", f"处理结果: {result_text}")
                                
                                if captcha_result:
                                    time.sleep(3)  # 等待验证码处理后的页面响应
                                else:
                                    print("[DEBUG] 验证码处理失败，继续等待...")
                                    if logger:
                                        logger.log_debug("验证码处理失败，继续等待...")
                            except Exception as e:
                                error_msg = f"验证码处理异常: {e}"
                                print(f"[ERROR] {error_msg}")
                                if logger:
                                    logger.log_exception(type(e).__name__, str(e), traceback.format_exc())
                        else:
                            print(f"[DEBUG] 第 {attempt} 次检查：未检测到验证码，当前URL: {page.url}")
                            if logger:
                                logger.log_debug(f"第 {attempt} 次检查：未检测到验证码")
                                logger.log_page_url(page.url)
                            
                            # 如果未检测到验证码，等待更长时间再检查（验证码可能需要时间加载）
                            if attempt < max_attempts:
                                wait_time = 2  # 等待2秒
                                print(f"[DEBUG] 等待 {wait_time} 秒后再次检查...")
                                time.sleep(wait_time)
                            else:
                                time.sleep(1)
                    
                    if not sign_success:
                        final_url = page.url
                        error_msg = f"签到失败：超时或验证码处理失败（已尝试 {max_attempts} 次）"
                        pending_failure_reason = error_msg
                        if logger:
                            logger.log_wait_timeout("签到循环", max_attempts, max_attempts)
                            logger.log_page_url(final_url)
            else:
                current_url_final = page.url
                error_msg = "未找到签到按钮"
                print(f"[ERROR] {error_msg}")
                print(f"[DEBUG] 当前URL: {current_url_final}")
                if logger:
                    logger.log_sign_failed(error_msg)
                    logger.log_page_url(current_url_final)
                    logger.log_debug("尝试查找其他可能的签到相关元素...")
                    
                    # 尝试查找其他可能的签到文本
                    try:
                        all_text = page.locator("body").inner_text()
                        if "签到" in all_text:
                            logger.log_debug("页面中包含'签到'文本，但未找到签到按钮")
                    except:
                        pass

        # 最终成功状态检查（包含“今日已签到”和“签到完成”）
        success_loc = find_signed_text_locator(page)
        final_completed = sign_success or (success_loc is not None) or is_checkin_completed(page, logger)

        # 最终失败前刷新一次页面再复核，避免状态延迟
        if not final_completed:
            try:
                page.reload(wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1200)
                success_loc = find_signed_text_locator(page)
                final_completed = sign_success or (success_loc is not None) or is_checkin_completed(page, logger)
                if logger:
                    logger.log_debug(f"最终刷新复核结果: final_completed={final_completed}")
            except Exception as e:
                if logger:
                    logger.log_debug(f"最终刷新复核失败: {e}")

        final_sign_button_visible = is_sign_button_visible(page, timeout=1200)
        final_success = (was_already_signed or final_completed) and (not final_sign_button_visible)

        if not final_success and final_sign_button_visible:
            failure_reason = "最终校验失败：签到按钮仍可见（判定为未签到）"
            print(f"[ERROR] {failure_reason}")
            if logger:
                logger.log_error(failure_reason)
        elif not final_success and pending_failure_reason:
            failure_reason = pending_failure_reason
            print(f"[ERROR] {failure_reason}")
            if logger:
                logger.log_sign_failed(failure_reason)
        elif final_success:
            failure_reason = ""
        if final_success:
            newly_signed_success = not was_already_signed
            parsed_after_traffic_bytes = get_remaining_traffic_bytes(page, logger)

            # 已签到场景：奖励固定为0
            if was_already_signed and reward_traffic_bytes is None:
                reward_traffic_bytes = 0

            # 新签到成功场景：优先重试获取奖励文案，并尝试等待/刷新后获取最新剩余流量
            if newly_signed_success and reward_traffic_bytes is None:
                for _ in range(3):
                    page.wait_for_timeout(1200)
                    reward_traffic_bytes = get_reward_traffic_bytes_from_page(page, logger)
                    if reward_traffic_bytes is not None:
                        break

            if newly_signed_success and before_traffic_bytes is not None:
                # 页面可能延迟刷新，先重试几次读取剩余流量
                for _ in range(3):
                    if parsed_after_traffic_bytes is not None and parsed_after_traffic_bytes > before_traffic_bytes:
                        break
                    page.wait_for_timeout(1200)
                    parsed_after_traffic_bytes = get_remaining_traffic_bytes(page, logger)

                # 仍未更新则触发一次轻量刷新再读
                if parsed_after_traffic_bytes is None or parsed_after_traffic_bytes <= before_traffic_bytes:
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=45000)
                        page.wait_for_timeout(1200)
                        parsed_after_traffic_bytes = get_remaining_traffic_bytes(page, logger)
                    except Exception:
                        pass

            # 若没有奖励文案，但签到后流量比签到前大，则差值即本次奖励
            if (
                reward_traffic_bytes is None
                and before_traffic_bytes is not None
                and parsed_after_traffic_bytes is not None
                and parsed_after_traffic_bytes > before_traffic_bytes
            ):
                reward_traffic_bytes = parsed_after_traffic_bytes - before_traffic_bytes

            # 优先使用“签到前 + 本次奖励”作为签到后流量，规避页面数据刷新延迟
            if before_traffic_bytes is not None and reward_traffic_bytes is not None:
                after_traffic_bytes = before_traffic_bytes + reward_traffic_bytes
                if logger:
                    logger.log_debug(
                        f"签到后流量使用计算值: {format_traffic(after_traffic_bytes)} "
                        f"(签到前 {format_traffic(before_traffic_bytes)} + 奖励 {format_traffic(reward_traffic_bytes)})"
                    )
            else:
                # 新签到成功但页面仍未刷新时，不要错误显示旧值
                if (
                    newly_signed_success
                    and before_traffic_bytes is not None
                    and parsed_after_traffic_bytes is not None
                    and parsed_after_traffic_bytes <= before_traffic_bytes
                ):
                    if logger:
                        logger.log_error("新签到成功后未能获取刷新后的剩余流量，签到后流量标记为未知")
                    parsed_after_traffic_bytes = None
                after_traffic_bytes = parsed_after_traffic_bytes

            if after_traffic_bytes is not None:
                print(f"[INFO] 签到后剩余流量: {format_traffic(after_traffic_bytes)}")
                if logger:
                    logger.log_info(f"签到后剩余流量: {format_traffic(after_traffic_bytes)}")

            if reward_traffic_bytes is not None:
                print(f"[INFO] 本次签到获得流量: {format_traffic(reward_traffic_bytes)}")
                if logger:
                    logger.log_info(f"本次签到获得流量: {format_traffic(reward_traffic_bytes)}")

        # 截图存证（如果需要）
        if save_screenshot:
            if final_success:
                try:
                    # 尝试截取父级区域，让截图更美观
                    if success_loc:
                        success_loc.locator(f"xpath=ancestor::*[{SIGNED_ANCESTOR_LEVELS}]").first.screenshot(path=str(SUCCESS_SCREENSHOT), timeout=10000)
                    else:
                        page.screenshot(path=str(SUCCESS_SCREENSHOT), timeout=10000)
                    print(f"[INFO] 截图已保存: {SUCCESS_SCREENSHOT}")
                except Exception as e:
                    print(f"[WARNING] 截图保存失败（不影响主流程）: {e}")
                    if logger:
                        logger.log_error(f"截图保存失败（不影响主流程）: {e}")

        # 邮件通知（成功发送成功邮件；最终失败可发送失败邮件）
        if final_success:
            run_success = True
            send_success_email(
                logger,
                before_traffic_bytes=before_traffic_bytes,
                reward_traffic_bytes=reward_traffic_bytes,
                after_traffic_bytes=after_traffic_bytes,
                account_name=username,
            )
        else:
            suppress_fail_email = os.getenv("SUPPRESS_FAIL_EMAIL", "false").strip().lower() in ("1", "true", "yes", "on")
            if not suppress_fail_email:
                send_failure_email(failure_reason, logger=logger, account_name=username)

        update_failure_alert_state(run_success, failure_reason, logger)
        
        print("[INFO] 脚本运行结束。")
        browser.close()

        return 0 if run_success else 1

if __name__ == "__main__":
    sys.exit(main())
