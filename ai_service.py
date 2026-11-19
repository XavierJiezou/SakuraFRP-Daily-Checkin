import base64
import json
import re
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from zhipuai import ZhipuAI

# 加载环境变量
load_dotenv()

# 题目名称中常见的前缀噪声（模型偶尔会返回整句话）
_TARGET_PREFIX_PATTERN = re.compile(
    r'^(图中是|图中的|图片中是|图片中的|图片里是|图片里的|这张图片?是|这是|该图是|答案是|物体名称是|名称是)'
)
_TARGET_QUANTIFIER_PATTERN = re.compile(r'^(一个|一只|一张|一把|一条|一台|一辆|一双|一部|一副)')

# 触发切换备用模型的错误特征：429（限流）、1305（模型繁忙）等
_BUSY_ERROR_KEYWORDS = ("429", "1305", "rate limit", "too many requests", "overload", "繁忙", "限流")


def is_model_busy_error(err):
    """判断异常是否为限流/模型繁忙类错误（这类错误重试主模型没用，换模型更快）"""
    parts = [str(err)]
    for attr in ("status_code", "code"):
        value = getattr(err, attr, None)
        if value is not None:
            parts.append(str(value))
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        parts.append(json.dumps(body, ensure_ascii=False))
    text = " ".join(parts).lower()
    return any(keyword in text for keyword in _BUSY_ERROR_KEYWORDS)


def clean_target_name(text):
    """把模型返回的题目文本收敛成一个干净的物体名称"""
    if not text:
        return ""

    name = text.strip().splitlines()[0].strip()
    for _ in range(3):
        new_name = _TARGET_PREFIX_PATTERN.sub("", name).strip()
        new_name = _TARGET_QUANTIFIER_PATTERN.sub("", new_name).strip()
        if new_name == name:
            break
        name = new_name

    name = re.sub(r'[^\w]', '', name)
    return name[:20]


class AIService:
    """AI服务类，封装所有AI调用逻辑"""

    def __init__(self):
        """初始化AI服务，从环境变量读取配置"""
        self.api_key = os.getenv("ZHIPU_API_KEY", "")
        self.model_vision = os.getenv("ZHIPU_MODEL_VISION", "glm-4.6v-flash")
        # 视觉备用模型：主模型限流/繁忙（429、1305）时自动切换
        self.model_vision_fallback = os.getenv("ZHIPU_MODEL_VISION_FALLBACK", "glm-4v-flash")
        self.model_text = os.getenv("ZHIPU_MODEL_TEXT", "glm-4.7-flash")

        try:
            self.max_retries = max(0, int(os.getenv("AI_CALL_MAX_RETRIES", "2").strip()))
        except Exception:
            self.max_retries = 2

        try:
            self.cell_workers = max(1, int(os.getenv("AI_CELL_WORKERS", "3").strip()))
        except Exception:
            self.cell_workers = 3

        if not self.api_key:
            raise ValueError("未找到ZHIPU_API_KEY环境变量，请在.env文件中配置")

        self.client = ZhipuAI(api_key=self.api_key)

    # ---------------- 基础调用 ----------------
    def _chat(self, model, messages, fallback_model=None):
        """带重试的对话调用，返回纯文本；失败返回空串。

        主模型遇到限流/模型繁忙（429、1305 等）时不再空转重试，立即切备用模型；
        其它错误在主模型重试耗尽后也会兜底切到备用模型。
        """
        models = [model]
        if fallback_model and fallback_model != model:
            models.append(fallback_model)

        last_error = None
        for index, current_model in enumerate(models):
            has_fallback = index + 1 < len(models)
            for i in range(self.max_retries + 1):
                try:
                    response = self.client.chat.completions.create(model=current_model, messages=messages)
                    if index > 0:
                        print(f"[INFO] 备用模型 {current_model} 调用成功")
                    return (response.choices[0].message.content or "").strip()
                except Exception as e:
                    last_error = e
                    print(f"[WARNING] AI 调用失败 {i + 1}/{self.max_retries + 1} "
                          f"({current_model}): {e}")
                    if has_fallback and is_model_busy_error(e):
                        print(f"[WARNING] {current_model} 限流/模型繁忙，切换备用模型: {models[index + 1]}")
                        break
                    if i < self.max_retries:
                        time.sleep(1.5 * (i + 1))

        print(f"[ERROR] AI API 调用失败 ({model}): {last_error}")
        return ""

    def safe_parse_json(self, text):
        """强力解析 AI 返回的 JSON 列表"""
        if not text:
            return None
        try:
            # 去掉 markdown 代码围栏，模型经常返回 ```json ... ```
            cleaned = re.sub(r'```[a-zA-Z]*', '', text).replace('```', '').strip()
            match = re.search(r'\[.*\]', cleaned, re.DOTALL)
            if match:
                return json.loads(match.group())
            return json.loads(cleaned)
        except Exception:
            return None

    def call_vision(self, image_bytes, prompt):
        """调用智谱多模态模型，image_bytes 支持单张图或图片列表"""
        images = image_bytes if isinstance(image_bytes, (list, tuple)) else [image_bytes]
        content = [{"type": "text", "text": prompt}]
        for img in images:
            if not img:
                continue
            b64 = base64.b64encode(img).decode('utf-8')
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})

        return self._chat(
            self.model_vision,
            [{"role": "user", "content": content}],
            fallback_model=self.model_vision_fallback,
        )

    # ---------------- 九宫格识别 ----------------
    def recognize_tip_object(self, tip_img_bytes):
        """识别题目提示图中的物体名称"""
        prompt = (
            "这是验证码的题目提示图，图中只有一个物体。\n"
            "只回答该物体的中文名称（2-6个字），不要句子、不要标点、不要解释。"
        )
        raw = self.call_vision(tip_img_bytes, prompt)
        target = clean_target_name(raw)
        print(f"[AI] 题目识别原始输出: {raw!r} -> 清洗后: {target!r}")
        return target

    def select_matching_cells(self, target, grid_img_bytes):
        """整图直选：把完整九宫格交给视觉模型，直接返回命中格子编号"""
        if not target:
            return []

        prompt = (
            f"这是一张 3x3 九宫格验证码图片，格子编号规则：第一行从左到右为 1、2、3，"
            f"第二行为 4、5、6，第三行为 7、8、9。\n"
            f"请找出其中内容是【{target}】的所有格子编号（同一物体的不同角度、不同材质、"
            f"玩具或模型也算命中）。\n"
            f"要求：只返回一个 JSON 整数数组，例如 [1, 5]；没有任何格子命中时返回 []；"
            f"不要输出解释文字。"
        )
        raw = self.call_vision(grid_img_bytes, prompt)
        print(f"[AI] 整图直选原始输出: {raw!r}")
        parsed = self.safe_parse_json(raw)
        return parsed if isinstance(parsed, list) else []

    def describe_cell(self, cell_img_bytes, cell_index):
        """识别单个格子的物体名称"""
        prompt = (
            "这是验证码九宫格中的一个格子。只回答格子里物体的中文名称（2-6个字）。\n"
            "如果格子是空白、纯色或看不出物体，只回答：空白。\n"
            "不要标点、不要解释、不要返回 JSON。"
        )
        raw = self.call_vision(cell_img_bytes, prompt)
        name = clean_target_name(raw) or "未知"
        print(f"[AI] 格子 {cell_index} 识别结果: {name}")
        return name

    def describe_cells(self, cell_img_bytes_list):
        """并发识别 9 个格子，返回按序排列的描述列表"""
        indexed = list(enumerate(cell_img_bytes_list, start=1))
        results = [None] * len(indexed)

        def worker(item):
            idx, img = item
            return idx, self.describe_cell(img, idx)

        with ThreadPoolExecutor(max_workers=self.cell_workers) as pool:
            for idx, name in pool.map(worker, indexed):
                results[idx - 1] = name

        return [r or "未知" for r in results]

    def identify_captcha_row(self, row_img_bytes, row_index):
        """分行识别逻辑（保留兼容接口，主流程已改为逐格识别）"""
        prompt = (
            "这是验证码的一行图片，从左到右包含3个格子。请分别识别这3个格子里的物体名称，"
            "只返回一个长度为3的 JSON 字符串数组，例如：[\"猫\", \"狗\", \"汽车\"]。"
            "看不出物体的格子填 \"空白\"。不要有任何解释文字。"
        )
        res = self.call_vision(row_img_bytes, prompt)
        print(f"[AI] 第 {row_index} 行识别结果: {res}")

        parsed = self.safe_parse_json(res)
        if parsed and isinstance(parsed, list):
            while len(parsed) < 3:
                parsed.append("未知")
            return parsed[:3]
        return ["未知", "未知", "未知"]

    def semantic_match(self, target, descriptions):
        """语义裁决逻辑：基于逐格描述挑出命中编号"""
        if not target:
            return []

        items_text = "\n".join([f"{i + 1}. {d}" for i, d in enumerate(descriptions)])
        prompt = (
            f"题目是：找出九宫格中所有的【{target}】。\n"
            f"9 个格子的识别描述如下：\n{items_text}\n"
            f"判断规则：描述与题目指向同一类物体即算命中（含同义词、上下位词、"
            f"玩具或模型形态）；描述为空白/未知的格子一律不算命中。\n"
            f"只返回 JSON 整数数组，如 [1, 3, 5]；没有命中返回 []。不要解释。"
        )

        print(f"[Debug] 正在进行语义裁决，描述列表：\n{items_text}")

        content = self._chat(self.model_text, [{"role": "user", "content": prompt}])
        print(f"[AI] 语义裁决原始输出: {content}")
        parsed = self.safe_parse_json(content)
        return parsed if isinstance(parsed, list) else []

    # ---------------- 滑块识别 ----------------
    def identify_slider_gap(self, bg_img_bytes, slice_img_bytes=None):
        """识别滑块验证码的缺口位置"""
        if slice_img_bytes:
            prompt = """这是滑块验证码的两张图片：
1. 第一张是完整的背景图
2. 第二张是带缺口的拼图块

请识别出缺口在背景图中的水平位置（x坐标，单位：像素）。
缺口位置是指拼图块应该放置的位置，即背景图中缺失的那部分对应的x坐标。

只返回一个数字，表示缺口位置的x坐标（像素值），不要有任何其他文字或解释。
例如：如果缺口在100像素的位置，只返回：100"""
            result_text = self.call_vision([bg_img_bytes, slice_img_bytes], prompt)
        else:
            prompt = """这是滑块验证码的背景图。请识别出图片中缺口的位置。
缺口通常是一个不规则的形状，与周围有明显的边缘差异。

请识别出缺口在图片中的水平位置（x坐标，单位：像素）。
只返回一个数字，表示缺口位置的x坐标（像素值），不要有任何其他文字或解释。
例如：如果缺口在100像素的位置，只返回：100"""
            result_text = self.call_vision(bg_img_bytes, prompt)

        print(f"[AI] 滑块缺口识别结果（原始）: {result_text}")

        numbers = re.findall(r'\d+', result_text or "")
        if numbers:
            gap_pos = int(numbers[0])
            print(f"[AI] 识别到的缺口位置: {gap_pos}px")
            return gap_pos

        print(f"[WARNING] 无法从AI结果中提取数字: {result_text}")
        return 0
