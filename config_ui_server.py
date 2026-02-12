from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

import requests
import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from yaml.representer import SafeRepresenter

# 导入数据库模块
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from trendradar.db import TaskDatabase, Task, TaskExecution
    HAS_DB = True
except ImportError:
    HAS_DB = False
    print("[警告] 数据库模块未找到，任务管理功能将不可用")

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
FREQUENCY_FILE = CONFIG_DIR / "frequency_words.txt"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
TIMELINE_FILE = CONFIG_DIR / "timeline.yaml"
SCHEDULE_FILE = CONFIG_DIR / "local_schedule.json"
UI_DIR = ROOT_DIR / "config_ui"
_RUN_LOCK = threading.Lock()

# --- Prompt ---
AI_SUGGEST_PROMPT = """你是一个专业的关键词扩展引擎。
用户会输入一个或多个公司名或主题。请为每个输入项生成一行对应的正则过滤规则。

【输出格式要求】
每一行必须严格遵守：/正则内容/ => 标注名

【生成逻辑】
1. 识别多个主体：如果用户输入“华为, 苹果”，请为每个主体生成一行。
2. 关键词扩展：包含核心产品、系统、高频人物等。
   - 华为：包含 华为、Huawei、Mate、鸿蒙、HarmonyOS、任正非 等。
   - 苹果：包含 苹果、Apple、iPhone、iPad、iOS、库克 等。
3. 正则规范：
   - 关键词之间用 | 分隔。
   - 英文单词（如 Apple）前后必须加 \b。
   - 不要解释，不要代码块，不要标题。

【正确输出示例】
/苹果|\\bApple\\b|iPhone|iPad|\\biOS\\b|库克/ => 苹果
/华为|\\bHuawei\\b|Mate|鸿蒙|任正非|\\bHarmonyOS\\b/ => 华为
"""

# 智能查询：AI 提取关键词的 Prompt
AI_EXTRACT_KEYWORDS_PROMPT = """你是一个专业的关键词提取与扩展引擎。
请从用户输入中提取核心主题，并为每个主题扩展相关的搜索词。

【输出格式要求】
严格返回 JSON，不要任何代码块标记：
{
  "display_keywords": ["华为"], 
  "search_keywords": ["华为", "Huawei", "Mate", "鸿蒙", "HarmonyOS", "任正非"],
  "confidence": 0.95
}

【提取规则】
1. 识别核心实体：人名、公司名、产品名、事件名等。
2. 忽略无效词：想、了解、最近、有什么、新闻等。
3. display_keywords：仅保留用户提到的原始核心实体，用于前端显示。
4. search_keywords：基于核心实体进行扩展。包含：
   - 品牌名（中英文）
   - 核心产品线（如 iPhone, Mate）
   - 关联系统或人物（如 iOS, 鸿蒙, 库克）
5. 如果用户输入过于模糊，search_keywords 保持与 display_keywords 一致，并降低 confidence。
6. 最多扩展 10 个搜索关键词。
"""

_yaml_roundtrip = YAML()
_yaml_roundtrip.preserve_quotes = True

# 兼容 ruamel 的 CommentedMap/CommentedSeq，避免在调用 pyyaml.safe_dump 时抛出
# "yaml.representer.RepresenterError: cannot represent an object"。
yaml.SafeDumper.add_representer(CommentedMap, SafeRepresenter.represent_dict)
yaml.SafeDumper.add_representer(CommentedSeq, SafeRepresenter.represent_list)


def _load_config() -> Dict[str, Any]:
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        data = _yaml_roundtrip.load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("config.yaml 格式异常")
    return data


def _save_config(data: Dict[str, Any]) -> None:
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        _yaml_roundtrip.dump(data, f)


def _load_ai_config() -> Dict[str, Any]:
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data.get("ai", {}) or {}


def _extract_header_lines(lines: List[str]) -> List[str]:
    header = []
    for line in lines:
        if line.strip().startswith("["):
            break
        if line.strip() == "" or line.lstrip().startswith("#"):
            header.append(line.rstrip("\n"))
            continue
        break
    return header


def _load_timeline_presets() -> List[str]:
    if not TIMELINE_FILE.exists():
        return ["always_on", "morning_evening", "office_hours", "night_owl", "custom"]

    with TIMELINE_FILE.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return ["always_on", "morning_evening", "office_hours", "night_owl", "custom"]

    presets = data.get("presets") or {}
    preset_names = list(presets.keys()) if isinstance(presets, dict) else []
    if "custom" not in preset_names:
        preset_names.append("custom")
    return preset_names


def _load_timeline_preset_details() -> Dict[str, Any]:
    if not TIMELINE_FILE.exists():
        return {}

    try:
        with TIMELINE_FILE.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    presets = data.get("presets") or {}
    if not isinstance(presets, dict):
        return {}

    details: Dict[str, Any] = {}
    for preset_key, preset_data in presets.items():
        if not isinstance(preset_data, dict):
            continue

        periods = preset_data.get("periods") or {}
        period_items: List[Dict[str, str]] = []
        if isinstance(periods, dict):
            for period_key, period_data in periods.items():
                if not isinstance(period_data, dict):
                    continue
                start = str(period_data.get("start") or "")
                end = str(period_data.get("end") or "")
                if not start or not end:
                    continue
                period_items.append(
                    {
                        "key": str(period_key),
                        "name": str(period_data.get("name") or period_key),
                        "start": start,
                        "end": end,
                    }
                )

        details[str(preset_key)] = {
            "name": str(preset_data.get("name") or preset_key),
            "description": str(preset_data.get("description") or ""),
            "periods": period_items,
            "default_push": bool((preset_data.get("default") or {}).get("push", False))
            if isinstance(preset_data.get("default"), dict)
            else False,
            "default_report_mode": str((preset_data.get("default") or {}).get("report_mode") or "")
            if isinstance(preset_data.get("default"), dict)
            else "",
        }

    return details


def _read_frequency_sections() -> Dict[str, str]:
    if not FREQUENCY_FILE.exists():
        return {"global_filter": "", "regex": ""}

    content = FREQUENCY_FILE.read_text(encoding="utf-8")
    lines = content.splitlines()

    current_section = None
    global_filter_lines: List[str] = []
    regex_lines: List[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "[GLOBAL_FILTER]":
            current_section = "GLOBAL_FILTER"
            continue
        if stripped == "[WORD_GROUPS]":
            current_section = "WORD_GROUPS"
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current_section = None
            continue

        if current_section == "GLOBAL_FILTER":
            global_filter_lines.append(line)
        elif current_section == "WORD_GROUPS":
            regex_lines.append(line)

    return {
        "global_filter": "\n".join(global_filter_lines).strip(),
        "regex": "\n".join(regex_lines).strip(),
    }


def _load_custom_plan() -> Dict[str, Any]:
    if not SCHEDULE_FILE.exists():
        return {
            "start": "09:00",
            "end": "18:00",
            "frequency_minutes": 60,
        }

    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid local schedule")
        return {
            "start": str(data.get("start") or "09:00"),
            "end": str(data.get("end") or "18:00"),
            "frequency_minutes": int(data.get("frequency_minutes") or 60),
        }
    except Exception:
        return {
            "start": "09:00",
            "end": "18:00",
            "frequency_minutes": 60,
        }


def _valid_hhmm(value: str) -> bool:
    return bool(re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value or ""))

# --- 修改：同时支持 GLOBAL_FILTER 和 WORD_GROUPS 写入 ---
def _build_frequency_content(regex: str, global_filter: str) -> str:
    existing_lines = FREQUENCY_FILE.read_text(encoding="utf-8").splitlines() if FREQUENCY_FILE.exists() else []
    header = _extract_header_lines(existing_lines)
    content_lines = []
    if header:
        content_lines.extend(header)
    content_lines.extend(
        [
            "",
            "[GLOBAL_FILTER]",
            global_filter.strip(),
            "",
            "[WORD_GROUPS]",
            regex.strip(),
            "",
        ]
    )
    return "\n".join(content_lines) + "\n"


def _detect_project_root() -> Path:
    """定位包含 trendradar 包的项目根目录。"""
    candidates = [ROOT_DIR, Path.cwd(), ROOT_DIR.parent]
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "trendradar" / "__init__.py").exists():
            return resolved
    return ROOT_DIR


def _extract_bat_config(bat_file: Path) -> Dict[str, str]:
    """从 bat 文件中提取 Python 路径和工作目录配置。"""
    try:
        content = bat_file.read_text(encoding="utf-8")
        python_path = None
        work_dir = None
        
        # 提取 Python 路径（格式如：/d/python/python-3.12/python.exe）
        python_match = re.search(r'(/[a-z]/[^"\s]+/python\.exe)', content)
        if python_match:
            # 将 Git Bash 路径转换为 Windows 路径
            git_path = python_match.group(1)
            # /d/python/python-3.12/python.exe -> D:\python\python-3.12\python.exe
            parts = git_path.split('/')
            if len(parts) >= 3 and len(parts[1]) == 1:
                drive = parts[1].upper()
                rest = '\\'.join(parts[2:])
                python_path = f"{drive}:\\{rest}"
        
        # 提取工作目录（格式如：cd /d/TRNNew）
        cd_match = re.search(r'cd\s+(/[a-z]/[^"\s;&]+)', content)
        if cd_match:
            git_path = cd_match.group(1)
            parts = git_path.split('/')
            if len(parts) >= 2 and len(parts[1]) == 1:
                drive = parts[1].upper()
                rest = '\\'.join(parts[2:]) if len(parts) > 2 else ""
                work_dir = f"{drive}:\\" + (rest if rest else "")
        
        return {
            "python_path": python_path or "",
            "work_dir": work_dir or "",
        }
    except Exception:
        return {"python_path": "", "work_dir": ""}


def _build_run_strategy(project_root: Path) -> Dict[str, Any]:
    """构建运行命令策略：精确模拟 bat 文件的运行方式。"""
    bat_file = project_root / "开始运行.bat"
    
    # Windows 环境：从 bat 文件中提取配置
    if os.name == "nt" and bat_file.exists():
        bat_config = _extract_bat_config(bat_file)
        
        # 如果能从 bat 中提取到配置，使用提取的配置
        if bat_config["python_path"] and bat_config["work_dir"]:
            python_exe = bat_config["python_path"]
            work_dir = bat_config["work_dir"]
            
            # 验证提取的路径是否存在
            if Path(python_exe).exists() and Path(work_dir).exists():
                # 关键修改：模拟 bat 文件的运行方式
                # bat 中使用: python.exe -c "import sys; sys.path.append('.'); import trendradar.__main__; trendradar.__main__.main()"
                # 这说明需要将当前目录加入 sys.path，然后直接导入运行
                return {
                    "name": "extracted_from_bat",
                    "command": [
                        python_exe,
                        "-c",
                        "import sys; sys.path.append('.'); import trendradar.__main__; trendradar.__main__.main()"
                    ],
                    "cwd": work_dir,
                    "env": {
                        **os.environ,
                        "PYTHONIOENCODING": "utf-8",
                    },
                }
        
        # 降级：尝试直接调用 bat（需要 Git Bash 环境）
        return {
            "name": "bat_script_direct",
            "command": [str(bat_file)],
            "cwd": str(project_root),
            "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
        }

    # 后备方案：直接使用 Python 模块运行
    pythonpath = str(project_root)
    if os.environ.get("PYTHONPATH"):
        pythonpath = pythonpath + os.pathsep + os.environ["PYTHONPATH"]

    return {
        "name": "python_module",
        "command": [sys.executable, "-m", "trendradar"],
        "cwd": str(project_root),
        "env": {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": pythonpath,
        },
    }


class ConfigRequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: Path) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_file(UI_DIR / "index.html")
            return
        if parsed.path == "/api/config":
            self._handle_get_config()
            return
        if parsed.path == "/api/tasks":
            self._handle_tasks_get()
            return
        if parsed.path.startswith("/api/tasks/"):
            self._handle_task_get_single()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "未找到资源")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/suggest":
            self._handle_suggest()
            return
        if parsed.path == "/api/run":
            self._handle_run()
            return
        if parsed.path == "/api/query/smart":
            self._handle_smart_query()
            return
        if parsed.path == "/api/search":
            self._handle_search()
            return
        if parsed.path == "/api/tasks":
            self._handle_tasks_post()
            return
        if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/execute"):
            self._handle_task_execute()
            return
        if parsed.path != "/api/config":
            self.send_error(HTTPStatus.NOT_FOUND, "未找到接口")
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        payload = json.loads(raw_body.decode("utf-8"))

        response: Dict[str, Any] = {"updated": []}

        # --- 核心修改：接收两个格子的内容 ---
        regex = (payload.get("regex") or "").strip()
        global_filter = (payload.get("global_filter") or "").strip()
        
        FREQUENCY_FILE.write_text(_build_frequency_content(regex, global_filter), encoding="utf-8")
        response["updated"].append("frequency_words.txt")

        schedule = payload.get("schedule") or {}
        if schedule:
            data = _load_config()
            schedule_config = data.setdefault("schedule", {})

            presets = _load_timeline_presets()
            preset = str(schedule.get("preset") or "morning_evening")
            if preset not in presets:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"detail": f"无效 schedule.preset: {preset}，可选: {', '.join(presets)}"},
                )
                return

            schedule_config["enabled"] = bool(schedule.get("enabled", True))
            schedule_config["preset"] = preset
            _save_config(data)
            response["updated"].append("config.yaml:schedule")

            custom_plan = payload.get("custom_plan") or {}
            if preset == "custom" and custom_plan:
                start = str(custom_plan.get("start") or "09:00")
                end = str(custom_plan.get("end") or "18:00")
                frequency_minutes = int(custom_plan.get("frequency_minutes") or 60)

                if not (_valid_hhmm(start) and _valid_hhmm(end)):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"detail": "自定义时段格式错误，请使用 HH:MM"},
                    )
                    return

                if frequency_minutes <= 0:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"detail": "发送频率必须为正整数（分钟）"},
                    )
                    return

                SCHEDULE_FILE.write_text(
                    json.dumps(
                        {
                            "start": start,
                            "end": end,
                            "frequency_minutes": frequency_minutes,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                response["updated"].append("local_schedule.json")

        report_mode = (payload.get("report_mode") or "").strip()
        if report_mode:
            if report_mode not in {"daily", "current", "incremental"}:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"detail": "report_mode 仅支持 daily/current/incremental"},
                )
                return

            data = _load_config()
            report = data.setdefault("report", {})
            report["mode"] = report_mode
            _save_config(data)
            response["updated"].append("config.yaml:report.mode")

        if "ai_analysis_enabled" in payload:
            data = _load_config()
            ai_analysis = data.setdefault("ai_analysis", {})
            ai_analysis["enabled"] = bool(payload.get("ai_analysis_enabled"))
            _save_config(data)
            response["updated"].append("config.yaml:ai_analysis.enabled")

        self._send_json(HTTPStatus.OK, response)

    def _handle_run(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            payload = {}

        if not _RUN_LOCK.acquire(blocking=False):
            self._send_json(
                HTTPStatus.CONFLICT,
                {"detail": "当前已有任务在运行，请稍后重试"},
            )
            return

        request_received_ts = time.time()
        try:
            project_root = _detect_project_root()
            strategy = _build_run_strategy(project_root)
            command = strategy["command"]
            process = subprocess.run(
                command,
                cwd=strategy["cwd"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=strategy["env"],
            )

            finished_ts = time.time()
            html_path = ""
            for line in process.stdout.splitlines():
                marker = "HTML报告已生成:"
                if marker in line:
                    html_path = line.split(marker, 1)[1].strip()

            client_sent_at_ms_raw = payload.get("client_sent_at_ms")
            front_to_html_ms = None
            if isinstance(client_sent_at_ms_raw, (int, float)) and client_sent_at_ms_raw > 0:
                front_to_html_ms = int((finished_ts * 1000) - float(client_sent_at_ms_raw))

            response_payload: Dict[str, Any] = {
                "success": process.returncode == 0,
                "returncode": process.returncode,
                "command": " ".join(command),
                "project_root": str(project_root),
                "run_strategy": strategy["name"],
                "timing": {
                    "backend_run_ms": int((finished_ts - request_received_ts) * 1000),
                    "frontend_to_html_ms": front_to_html_ms,
                    "request_received_at": request_received_ts,
                    "finished_at": finished_ts,
                },
                "html_path": html_path,
                "stdout": process.stdout,
                "stderr": process.stderr,
            }
            status = HTTPStatus.OK if process.returncode == 0 else HTTPStatus.BAD_GATEWAY
            self._send_json(status, response_payload)
        finally:
            _RUN_LOCK.release()

    def _handle_get_config(self) -> None:
        data = _load_config()
        schedule = data.get("schedule") or {}
        report = data.get("report") or {}
        ai_analysis = data.get("ai_analysis") or {}
        freq = _read_frequency_sections()

        self._send_json(
            HTTPStatus.OK,
            {
                "regex": freq.get("regex", ""),
                "global_filter": freq.get("global_filter", ""),
                "schedule": {
                    "enabled": bool(schedule.get("enabled", True)),
                    "preset": schedule.get("preset", "morning_evening"),
                    "presets": _load_timeline_presets(),
                    "preset_details": _load_timeline_preset_details(),
                },
                "custom_plan": _load_custom_plan(),
                "report_mode": report.get("mode", "current"),
                "ai_analysis_enabled": bool(ai_analysis.get("enabled", True)),
            },
        )

    def _handle_suggest(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        payload = json.loads(raw_body.decode("utf-8"))
        keyword = (payload.get("keyword") or "").strip()

        ai_config = _load_ai_config()
        api_key = ai_config.get("api_key", "")
        model = ai_config.get("model", "")
        api_base = ai_config.get("api_base", "") or "https://api.openai.com/v1"
        
        clean_model = model.replace("openai/", "")
        url = api_base.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        body = {
            "model": clean_model,
            "messages": [
                {"role": "system", "content": AI_SUGGEST_PROMPT},
                {"role": "user", "content": f"主题词：{keyword}"},
            ],
            "temperature": 0.3
        }

        try:
            resp = requests.post(url, headers=headers, json=body, timeout=60)
            data = resp.json()
            # 直接提取 AI 的完整回复内容，不再做单行正则提取
            content = data["choices"][0]["message"]["content"].strip()
            self._send_json(HTTPStatus.OK, {"regex": content})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"detail": str(exc)})

    def _handle_smart_query(self) -> None:
        """处理智能查询请求"""
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        payload = json.loads(raw_body.decode("utf-8"))
        
        user_query = (payload.get("query") or "").strip()
        if not user_query:
            self._send_json(HTTPStatus.BAD_REQUEST, {"detail": "查询内容不能为空"})
            return
        
        start_time = time.time()
        
        # 步骤1: 使用 AI 提取关键词
        ai_config = _load_ai_config()
        api_key = ai_config.get("api_key", "")
        model = ai_config.get("model", "")
        api_base = ai_config.get("api_base", "") or "https://api.openai.com/v1"
        
        clean_model = model.replace("openai/", "")
        url = api_base.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        
        # AI 提取关键词
        extract_body = {
            "model": clean_model,
            "messages": [
                {"role": "system", "content": AI_EXTRACT_KEYWORDS_PROMPT},
                {"role": "user", "content": user_query},
            ],
            "temperature": 0.3
        }
        
        try:
            # 调用 AI 提取关键词
            resp = requests.post(url, headers=headers, json=extract_body, timeout=60)
            ai_response = resp.json()
            ai_content = ai_response["choices"][0]["message"]["content"].strip()
            
            # 解析 JSON 响应
            # 移除可能的 markdown 代码块标记
            ai_content = ai_content.replace("```json", "").replace("```", "").strip()
            keywords_data = json.loads(ai_content)
            
            # 获取用于前端显示的词
            display_keywords = keywords_data.get("display_keywords", [])
            # 获取用于实际搜索的词
            search_keywords = keywords_data.get("search_keywords", display_keywords)
            confidence = keywords_data.get("confidence", 0.0)

            print("\n" + "="*50)
            print(f"[智能查询日志] 用户输入: {user_query}")
            print(f"前端显示关键词: {display_keywords}")
            print(f"后端搜索关键词: {search_keywords}")
            print(f"提取置信度: {confidence}")
            print("="*50 + "\n")
            
            if not search_keywords:
                self._send_json(HTTPStatus.OK, {
                    "success": False,
                    "message": "无法从您的描述中提取有效关键词，请尝试更具体的描述",
                    "extracted_keywords": [],
                    "confidence": confidence,
                    "results": [],
                    "timing": {
                        "total_ms": int((time.time() - start_time) * 1000)
                    }
                })
                return
            
            # 步骤2: 使用提取的关键词搜索数据
            sys.path.insert(0, str(ROOT_DIR))
            from trendradar.crawler.fetcher import DataFetcher
            from trendradar.crawler.rss.fetcher import RSSFetcher
                
            # 读取配置，获取平台列表
            config_data = _load_config()
            
            # === 搜索热搜平台 ===
            platforms_config = config_data.get("platforms", {})
            if platforms_config.get("enabled", True):
                platform_sources = platforms_config.get("sources", [])
                # 使用所有启用的平台（不再限制5个）
                platform_ids = [(p["id"], p["name"]) for p in platform_sources]
                
                print(f"[智能查询] 开始搜索 {len(platform_ids)} 个热搜平台...")
                fetcher = DataFetcher()
                hotlist_results, id_to_name, failed_ids = fetcher.crawl_websites(
                    platform_ids, 
                    request_interval=100  # 100ms 间隔，避免太慢
                )
            else:
                hotlist_results = {}
                id_to_name = {}
                failed_ids = []
            
            # === 搜索 RSS 源 ===
            rss_results = {}
            rss_id_to_name = {}
            rss_config = config_data.get("rss", {})
            if rss_config.get("enabled", True):
                feeds = rss_config.get("feeds", [])
                if feeds:
                    print(f"[智能查询] 开始搜索 {len(feeds)} 个 RSS 源...")
                    # 创建 RSS Fetcher
                    rss_fetcher = RSSFetcher.from_config(rss_config)
                    rss_data = rss_fetcher.fetch_all()
                    
                    # 转换 RSS 数据格式为类似热搜平台的格式
                    for feed_id, items in rss_data.items.items():
                        rss_results[feed_id] = {}
                        rss_id_to_name[feed_id] = rss_data.id_to_name.get(feed_id, feed_id)
                        
                        for item in items:
                            title = item.title
                            # 构造类似热搜平台的数据结构
                            rss_results[feed_id][title] = {
                                "ranks": [],  # RSS 没有排名
                                "url": item.url,
                                "mobileUrl": item.url,
                                "published_at": item.published_at,
                            }
            
            # === 合并结果 ===
            all_results = {**hotlist_results, **rss_results}
            all_id_to_name = {**id_to_name, **rss_id_to_name}
            
            # 步骤3: 过滤匹配关键词的新闻
            matched_items = []
            # 预处理：确保所有搜索词都是字符串并小写
            search_keywords_lower = [str(k).lower() for k in search_keywords if k]

            for source_id, news_dict in all_results.items():
                source_name = all_id_to_name.get(source_id, source_id)
                source_type = "RSS" if source_id in rss_results else "热搜平台"
                
                # 判空保护：确保 news_dict 是字典
                if not isinstance(news_dict, dict):
                    continue

                for title, info in news_dict.items():
                    # 1. 安全处理标题（防止 None 对象引发 AttributeError）
                    safe_title = str(title or "")
                    title_lower = safe_title.lower()
                    
                    # 2. 执行匹配判断
                    if any(kw in title_lower for kw in search_keywords_lower):
                        matched_items.append({
                            "title": safe_title,
                            "platform": source_name,
                            "platform_id": source_id,
                            "source_type": source_type,
                            "url": info.get("url", ""),
                            "mobile_url": info.get("mobileUrl", ""),
                            "ranks": info.get("ranks", []),
                            "published_at": info.get("published_at", ""),
                        })
            
            # 按匹配度排序（排名越多的越靠前，RSS文章按发布时间）
            def sort_key(item):
                if item["ranks"]:
                    # 热搜平台：按排名数量
                    return (1, -len(item["ranks"]), min(item["ranks"]) if item["ranks"] else 999)
                else:
                    # RSS：按发布时间（越新越靠前）
                    return (2, item.get("published_at", ""), 0)
            
            matched_items.sort(key=sort_key)
            
            # 限制返回数量（可调整）
            max_results = 100  # 增加到100条
            matched_items = matched_items[:max_results]
            
            end_time = time.time()
            
            # 统计搜索的平台
            searched_platforms = []
            if hotlist_results:
                searched_platforms.extend([f"{name}(热搜)" for name in id_to_name.values()])
            if rss_results:
                searched_platforms.extend([f"{name}(RSS)" for name in rss_id_to_name.values()])
            
            self._send_json(HTTPStatus.OK, {
                "success": True,
                "query": user_query,
                "extracted_keywords": display_keywords,  # 前端只显示核心词
                "search_keywords_count": len(search_keywords), 
                "confidence": confidence,
                "results": matched_items,
                "total_count": len(matched_items),
                "searched_platforms": searched_platforms,
                "stats": {
                    "hotlist_count": len(hotlist_results),
                    "rss_count": len(rss_results),
                },
                "timing": {
                    "total_ms": int((end_time - start_time) * 1000),
                }
            })
            
        except json.JSONDecodeError as e:
            self._send_json(HTTPStatus.BAD_GATEWAY, {
                "success": False,
                "detail": f"AI 返回格式错误: {e}",
                "raw_response": ai_content if 'ai_content' in locals() else ""
            })
        except Exception as exc:
            import traceback
            traceback.print_exc()  # 打印详细错误信息到控制台
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "success": False,
                "detail": f"查询失败: {str(exc)}"
            })

    def _handle_search(self) -> None:
        """
        服务化搜索接口
        
        执行完整的 TrendRadar 流程（抓取→分析→生成HTML）
        """
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            decoded_body = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            decoded_body = raw_body.decode("gbk") # 如果 utf-8 失败，尝试 gbk
        payload = json.loads(decoded_body)
        
        # 参数验证
        keywords = payload.get("keywords")
        if not keywords or not isinstance(keywords, list) or len(keywords) == 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "success": False,
                "detail": "keywords 必填，且必须是非空数组"
            })
            return
        
        generate_html = payload.get("generate_html")
        if generate_html is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "success": False,
                "detail": "generate_html 必填（true/false）"
            })
            return
        
        filters = payload.get("filters", [])
        platforms = payload.get("platforms", None)
        report_mode = payload.get("report_mode", "current")
        user_id = payload.get("user_id", None)
        expand_keywords = payload.get("expand_keywords", True)  # 默认启用AI扩展
        
        start_time = time.time()
        
        # 备份原始配置文件
        freq_file_backup = None
        config_backup = None
        
        try:
            print(f"[服务化搜索] 开始执行 - 关键词: {keywords}, 过滤词: {filters}")
            
            # 1. AI 扩展关键词（如果启用）
            if expand_keywords:
                expanded_keywords = self._expand_keywords_with_ai(keywords)
            else:
                expanded_keywords = keywords
                print(f"[服务化搜索] AI扩展已禁用，使用原始关键词")
            
            # 2. 备份并临时修改 frequency_words.txt
            if FREQUENCY_FILE.exists():
                freq_file_backup = FREQUENCY_FILE.read_text(encoding="utf-8")
            
            temp_freq_content = self._build_temp_frequency_content(expanded_keywords, filters)
            FREQUENCY_FILE.write_text(temp_freq_content, encoding="utf-8")
            
            # 2. 备份并临时修改 config.yaml（如果需要限制平台）
            if platforms:
                config_data = _load_config()
                config_backup = json.dumps(config_data)  # 备份
                
                # 修改platforms配置
                if "platforms" in config_data:
                    original_sources = config_data["platforms"].get("sources", [])
                    filtered_sources = [s for s in original_sources if s["id"] in platforms]
                    config_data["platforms"]["sources"] = filtered_sources
                
                # 修改report mode
                if "report" not in config_data:
                    config_data["report"] = {}
                config_data["report"]["mode"] = report_mode
                
                _save_config(config_data)
            else:
                # 只修改 report mode
                config_data = _load_config()
                config_backup = json.dumps(config_data)
                if "report" not in config_data:
                    config_data["report"] = {}
                config_data["report"]["mode"] = report_mode
                _save_config(config_data)
            
            # 3. 运行主程序（模拟 python -m trendradar）
            sys.path.insert(0, str(ROOT_DIR))
            from trendradar.__main__ import NewsAnalyzer
            from trendradar.core import load_config
            
            # 重新加载配置（使用修改后的）
            config = load_config()
            
            # 创建分析器
            analyzer = NewsAnalyzer(config=config)
            
            # 禁用浏览器自动打开
            analyzer.is_docker_container = True
            
            # 执行完整的分析流程（抓取+分析+生成HTML）
            analyzer.run()
            
            # 从存储中获取最新生成的HTML路径
            # run() 方法内部会生成HTML，我们需要找到它
            import sqlite3
            from pathlib import Path
            
            # 查找最新生成的HTML文件
            output_html_dir = ROOT_DIR / "output" / "html"
            latest_html = None
            
            # 方式1：查找latest目录
            latest_dir = output_html_dir / "latest"
            if latest_dir.exists():
                mode_html = latest_dir / f"{report_mode}.html"
                if mode_html.exists():
                    latest_html = str(mode_html)
            
            # 方式2：如果latest不存在，找最新的时间戳文件
            if not latest_html:
                from datetime import datetime
                date_folder = output_html_dir / datetime.now().strftime("%Y-%m-%d")
                if date_folder.exists():
                    html_files = sorted(date_folder.glob("*.html"), key=lambda x: x.stat().st_mtime, reverse=True)
                    if html_files:
                        latest_html = str(html_files[0])
            
            end_time = time.time()
            
            if generate_html:
                if latest_html:
                    # 计算相对路径
                    html_path_obj = Path(latest_html)
                    try:
                        relative_path = html_path_obj.relative_to(ROOT_DIR)
                    except ValueError:
                        relative_path = html_path_obj
                    
                    print(f"[服务化搜索] HTML已生成: {latest_html}")
                    
                    self._send_json(HTTPStatus.OK, {
                        "success": True,
                        "html_url": str(relative_path).replace("\\", "/"),
                        "html_path": str(html_path_obj),
                        "duration_ms": int((end_time - start_time) * 1000),
                        "stats": {
                            "platforms_count": len(config.get("PLATFORMS", [])),
                        }
                    })
                else:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                        "success": False,
                        "detail": "HTML生成失败"
                    })
            else:
                # 返回链接列表模式
                # 需要从最新生成的数据库中提取链接-
                from trendradar.core.frequency import load_frequency_words, matches_word_groups
                from datetime import datetime
                
                word_groups, filter_words, global_filters = load_frequency_words(str(FREQUENCY_FILE))
                
                # 从存储中读取最新数据
                links = []
                try:
                    all_rows = []
                    from datetime import datetime
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    
                    # 1. 查询 News 数据库
                    news_db = ROOT_DIR / "output" / "news" / f"{today_str}.db"
                    if news_db.exists():
                        with sqlite3.connect(str(news_db)) as conn:
                            cursor = conn.cursor()
                            cursor.execute("""
                                SELECT title, platform_id, url, mobile_url 
                                FROM news_items 
                                ORDER BY id DESC LIMIT 2000
                            """)
                            all_rows.extend(cursor.fetchall())

                    # 2. 查询 RSS 数据库
                    rss_db = ROOT_DIR / "output" / "rss" / f"{today_str}.db"
                    if rss_db.exists():
                        with sqlite3.connect(str(rss_db)) as conn:
                            cursor = conn.cursor()
                            cursor.execute("""
                                SELECT title, feed_id, url, '' as mobile_url 
                                FROM rss_items 
                                ORDER BY id DESC LIMIT 2000
                            """)
                            all_rows.extend(cursor.fetchall())

                    print(f"[服务化搜索] 从数据库读取到 {len(all_rows)} 条原始数据，准备匹配关键词...")

                    # 3. 开始关键词过滤
                    for row in all_rows:
                        title, source_id, url, mobile_url = row
    
                        # 只要标题里包含了 AI 扩展后的任意一个关键词，就认为匹配
                        matched_keyword = None
                        title_lower = title.lower()
    
                        for kw in expanded_keywords:
                            if kw.lower() in title_lower:
                                matched_keyword = kw
                                break
            
                        if matched_keyword:
                            links.append({
                                "title": title,
                                "url": url or "",
                                "mobile_url": mobile_url or "",
                                "platform": source_id,
                                "keyword": matched_keyword,
                            })
                except Exception as e:
                    print(f"[服务化搜索] 读取链接失败: {e}")
                
                end_time = time.time()
                
                print(f"[服务化搜索] 完成，返回 {len(links)} 条链接")
                
                self._send_json(HTTPStatus.OK, {
                    "success": True,
                    "links": links,
                    "duration_ms": int((end_time - start_time) * 1000),
                    "stats": {
                        "matched_links": len(links),
                        "platforms_count": len(config.get("PLATFORMS", [])),
                    }
                })

        except Exception as exc:
            import traceback
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "success": False,
                "detail": f"搜索失败: {str(exc)}"
            })
        finally:
            # 恢复原始配置
            if freq_file_backup is not None:
                FREQUENCY_FILE.write_text(freq_file_backup, encoding="utf-8")
                print("[服务化搜索] 已恢复 frequency_words.txt")

            if config_backup is not None:
                config_data = json.loads(config_backup)
                _save_config(config_data)
                print("[服务化搜索] 已恢复 config.yaml")
    
    def _expand_keywords_with_ai(self, keywords: List[str]) -> List[str]:
        """
        使用 AI 扩展关键词
        
        例如："苹果" → "苹果|Apple|iPhone|iPad|iOS|库克"
        """
        ai_config = _load_ai_config()
        api_key = ai_config.get("api_key", "")
        model = ai_config.get("model", "")
        api_base = ai_config.get("api_base", "") or "https://api.openai.com/v1"
        
        clean_model = model.replace("openai/", "")
        url = api_base.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        
        # AI 扩展提示词
        expand_prompt = """你是一个专业的关键词扩展引擎。
用户会输入一个或多个主题词，请为每个主题扩展相关的搜索关键词。

【输出格式要求】
严格返回 JSON 格式，不要任何代码块标记：
{
  "expanded": [
    {
      "original": "苹果",
      "keywords": ["苹果", "Apple", "iPhone", "iPad", "iOS", "库克", "Tim Cook"]
    }
  ]
}

【扩展规则】
1. 保留原始关键词
2. 添加英文名称（如果有）
3. 添加核心产品线
4. 添加关联人物（CEO、创始人等）
5. 添加关联技术或系统
6. 每个主题最多扩展 10 个关键词

【示例】
输入：["华为", "比亚迪"]
输出：
{
  "expanded": [
    {
      "original": "华为",
      "keywords": ["华为", "Huawei", "Mate", "鸿蒙", "HarmonyOS", "任正非"]
    },
    {
      "original": "比亚迪",
      "keywords": ["比亚迪", "BYD", "秦", "汉", "唐", "王传福", "刀片电池"]
    }
  ]
}
"""
        
        try:
            body = {
                "model": clean_model,
                "messages": [
                    {"role": "system", "content": expand_prompt},
                    {"role": "user", "content": f"主题词：{json.dumps(keywords, ensure_ascii=False)}"},
                ],
                "temperature": 0.3
            }
            
            print(f"[AI扩展] 正在扩展关键词: {keywords}")
            
            resp = requests.post(url, headers=headers, json=body, timeout=60)
            ai_response = resp.json()
            ai_content = ai_response["choices"][0]["message"]["content"].strip()
            
            # 移除可能的代码块标记
            ai_content = ai_content.replace("```json", "").replace("```", "").strip()
            result = json.loads(ai_content)
            
            expanded_keywords = []
            for item in result.get("expanded", []):
                original = item.get("original", "")
                kws = item.get("keywords", [])
                
                # 构建正则表达式格式
                # 中文词直接拼接，英文词加 \b
                regex_parts = []
                for kw in kws:
                    if re.match(r'^[a-zA-Z\s]+$', kw):  # 纯英文
                        # 多词英文需要转义空格
                        kw_escaped = kw.replace(' ', r'\s+')
                        regex_parts.append(f"\\b{kw_escaped}\\b")
                    else:  # 中文或混合
                        regex_parts.append(kw)
                
                # 构建正则表达式：/词A|词B|词C/ => 原始词
                regex_pattern = f"/{('|'.join(regex_parts))}/ => {original}"
                expanded_keywords.append(regex_pattern)
                
                print(f"[AI扩展] {original} → {', '.join(kws)}")
            
            return expanded_keywords
            
        except Exception as e:
            print(f"[AI扩展] 扩展失败: {e}，使用原始关键词")
            # 失败时返回原始关键词
            return keywords
    
    def _build_temp_frequency_content(self, keywords: List[str], filters: List[str]) -> str:
        """构建临时的 frequency_words.txt 内容"""
        lines = []
        
        # 添加全局过滤词
        if filters:
            lines.append("[GLOBAL_FILTER]")
            for f in filters:
                lines.append(f)
            lines.append("")
        
        # 添加关键词组
        lines.append("[WORD_GROUPS]")
        for keyword in keywords:
            lines.append(keyword)
            lines.append("")  # 词组之间空行分隔
        
        return "\n".join(lines)
    
    # ============ 任务管理 API ============
    
    def _handle_tasks_get(self) -> None:
        """GET /api/tasks - 获取用户任务列表"""
        if not HAS_DB:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "success": False,
                "detail": "数据库模块未初始化"
            })
            return
        
        # 从查询参数获取 user_id
        from urllib.parse import parse_qs
        query = parse_qs(urlparse(self.path).query)
        user_id = query.get('user_id', [None])[0]
        
        if not user_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "success": False,
                "detail": "缺少 user_id 参数"
            })
            return
        
        try:
            db = TaskDatabase()
            
            # 确保用户存在
            db.get_or_create_user(user_id)
            
            # 获取用户任务
            tasks = db.get_user_tasks(user_id)
            
            self._send_json(HTTPStatus.OK, {
                "success": True,
                "tasks": [task.to_dict() for task in tasks],
                "total": len(tasks)
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "success": False,
                "detail": f"查询失败: {str(e)}"
            })
    
    def _handle_task_get_single(self) -> None:
        """GET /api/tasks/{task_id} - 获取单个任务详情"""
        if not HAS_DB:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "success": False,
                "detail": "数据库模块未初始化"
            })
            return
        
        # 从路径提取 task_id
        task_id = self.path.split('/')[-1].split('?')[0]
        
        try:
            db = TaskDatabase()
            task = db.get_task(task_id)
            
            if not task:
                self._send_json(HTTPStatus.NOT_FOUND, {
                    "success": False,
                    "detail": "任务不存在"
                })
                return
            
            # 获取执行历史
            executions = db.get_task_executions(task_id, limit=5)
            
            self._send_json(HTTPStatus.OK, {
                "success": True,
                "task": task.to_dict(),
                "executions": [ex.to_dict() for ex in executions]
            })
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "success": False,
                "detail": f"查询失败: {str(e)}"
            })
    
    def _handle_tasks_post(self) -> None:
        """POST /api/tasks - 创建或更新任务"""
        if not HAS_DB:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "success": False,
                "detail": "数据库模块未初始化"
            })
            return
        
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        try:
            # Try the standard UTF-8 first
            decoded_body = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            # If it fails, it's almost certainly GBK (common in Windows terminals)
            try:
                decoded_body = raw_body.decode("gbk")
            except UnicodeDecodeError:
                # Fallback to 'replace' to prevent the whole server from crashing
                decoded_body = raw_body.decode("utf-8", errors="replace")

        payload = json.loads(decoded_body)
        
        user_id = payload.get("user_id")
        task_id = payload.get("task_id")  # 如果提供task_id，则更新；否则创建
        
        if not user_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "success": False,
                "detail": "缺少 user_id 参数"
            })
            return
        
        try:
            db = TaskDatabase()
            
            # 确保用户存在
            db.get_or_create_user(user_id)
            
            if task_id:
                # 更新现有任务
                existing_task = db.get_task(task_id)
                if not existing_task:
                    self._send_json(HTTPStatus.NOT_FOUND, {
                        "success": False,
                        "detail": "任务不存在"
                    })
                    return
                
                # 检查权限（只能更新自己的任务）
                if existing_task.user_id != user_id:
                    self._send_json(HTTPStatus.FORBIDDEN, {
                        "success": False,
                        "detail": "无权限修改此任务"
                    })
                    return
                
                # 构建更新字典
                updates = {}
                for key in ['name', 'keywords', 'filters', 'platforms', 'report_mode', 
                           'schedule', 'expand_keywords', 'status', 'description']:
                    if key in payload:
                        updates[key] = payload[key]
                
                success = db.update_task(task_id, updates)
                
                if success:
                    task = db.get_task(task_id)
                    self._send_json(HTTPStatus.OK, {
                        "success": True,
                        "message": "任务已更新",
                        "task": task.to_dict()
                    })
                else:
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                        "success": False,
                        "detail": "更新失败"
                    })
            else:
                # 创建新任务
                name = payload.get("name")
                keywords = payload.get("keywords", [])
                
                if not name or not keywords:
                    self._send_json(HTTPStatus.BAD_REQUEST, {
                        "success": False,
                        "detail": "name 和 keywords 必填"
                    })
                    return
                
                task = Task.from_dict({
                    "name": name,
                    "user_id": user_id,
                    "keywords": keywords,
                    "filters": payload.get("filters", []),
                    "platforms": payload.get("platforms", []),
                    "report_mode": payload.get("report_mode", "current"),
                    "schedule": payload.get("schedule"),
                    "expand_keywords": payload.get("expand_keywords", True),
                    "description": payload.get("description")
                })
                
                created_task = db.create_task(task)
                
                self._send_json(HTTPStatus.CREATED, {
                    "success": True,
                    "message": "任务已创建",
                    "task": created_task.to_dict()
                })
                
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "success": False,
                "detail": f"操作失败: {str(e)}"
            })
    
    def _execute_search_task(
        self,
        keywords: List[str],
        filters: List[str] = None,
        platforms: List[str] = None,
        report_mode: str = "current",
        expand_keywords: bool = True,
        generate_html: bool = True,
        task_id: str = None
    ) -> Dict:
        """
        执行搜索任务（内部函数，可被 /api/search 和任务执行复用）
        
        Returns:
            搜索结果字典
        """
        start_time = time.time()
        
        # 备份原始配置
        freq_file_backup = None
        config_backup = None
        
        try:
            print(f"[执行搜索] 关键词: {keywords}, 过滤词: {filters}")
            
            # 1. AI 扩展关键词（如果启用）
            if expand_keywords:
                expanded_keywords = self._expand_keywords_with_ai(keywords)
            else:
                expanded_keywords = keywords
            
            # 2. 备份并临时修改配置
            if FREQUENCY_FILE.exists():
                freq_file_backup = FREQUENCY_FILE.read_text(encoding="utf-8")
            
            temp_freq_content = self._build_temp_frequency_content(expanded_keywords, filters or [])
            FREQUENCY_FILE.write_text(temp_freq_content, encoding="utf-8")
            
            # 3. 修改 config.yaml
            config_data = _load_config()
            config_backup = json.dumps(config_data)
            
            if platforms:
                if "platforms" in config_data:
                    original_sources = config_data["platforms"].get("sources", [])
                    filtered_sources = [s for s in original_sources if s["id"] in platforms]
                    config_data["platforms"]["sources"] = filtered_sources
            
            if "report" not in config_data:
                config_data["report"] = {}
            config_data["report"]["mode"] = report_mode
            
            _save_config(config_data)
            
            # 4. 运行分析
            sys.path.insert(0, str(ROOT_DIR))
            from trendradar.__main__ import NewsAnalyzer
            from trendradar.core import load_config
            
            config = load_config()

            # 强制禁用调度系统，防止 daily 被 current 覆盖
            if "schedule" in config:
                config["schedule"]["enabled"] = False
                print("[服务化搜索] 已临时禁用调度系统以维持请求模式")

            analyzer = NewsAnalyzer(config=config)
            analyzer.is_docker_container = True
            analyzer.run()
            
            # 5. 查找生成的HTML
            output_html_dir = ROOT_DIR / "output" / "html"
            latest_html = None
            
            latest_dir = output_html_dir / "latest"
            if latest_dir.exists():
                mode_html = latest_dir / f"{report_mode}.html"
                if mode_html.exists():
                    latest_html = str(mode_html)
            
            if not latest_html:
                from datetime import datetime
                date_folder = output_html_dir / datetime.now().strftime("%Y-%m-%d")
                if date_folder.exists():
                    html_files = sorted(date_folder.glob("*.html"), 
                                      key=lambda x: x.stat().st_mtime, reverse=True)
                    if html_files:
                        latest_html = str(html_files[0])
            
            end_time = time.time()
            duration_ms = int((end_time - start_time) * 1000)
            
            # 6. 构建返回结果
            result = {
                "success": True,
                "duration_ms": duration_ms,
                "stats": {
                    "platforms_count": len(config.get("PLATFORMS", [])),
                }
            }
            
            if generate_html:
                if latest_html:
                    html_path_obj = Path(latest_html)
                    try:
                        relative_path = html_path_obj.relative_to(ROOT_DIR)
                    except ValueError:
                        relative_path = html_path_obj
                    
                    result["html_url"] = str(relative_path).replace("\\", "/")
                    result["html_path"] = str(html_path_obj)
                    
                    print(f"[执行搜索] HTML已生成: {latest_html}")
                else:
                    result["success"] = False
                    result["detail"] = "HTML生成失败"
            else:
                # 返回链接列表（暂不实现，保持简单）
                result["links"] = []
            
            # 7. 如果有task_id，记录执行历史
            if task_id and HAS_DB:
                try:
                    db = TaskDatabase()
                    execution = TaskExecution(
                        task_id=task_id,
                        html_path=result.get("html_path"),
                        matched_count=0,  # TODO: 从结果中提取
                        duration_ms=duration_ms,
                        status="success" if result["success"] else "failed",
                        error_message=result.get("detail")
                    )
                    db.add_execution(execution)
                except Exception as e:
                    print(f"[执行搜索] 保存执行记录失败: {e}")
            
            return result
            
        except Exception as exc:
            import traceback
            traceback.print_exc()
            
            # 记录失败
            if task_id and HAS_DB:
                try:
                    db = TaskDatabase()
                    execution = TaskExecution(
                        task_id=task_id,
                        status="failed",
                        error_message=str(exc),
                        duration_ms=int((time.time() - start_time) * 1000)
                    )
                    db.add_execution(execution)
                except:
                    pass
            
            return {
                "success": False,
                "detail": f"搜索失败: {str(exc)}"
            }
        finally:
            # 恢复配置
            if freq_file_backup is not None:
                FREQUENCY_FILE.write_text(freq_file_backup, encoding="utf-8")
            
            if config_backup is not None:
                config_data = json.loads(config_backup)
                _save_config(config_data)
    
    def _handle_search(self) -> None:
        """POST /api/search - 服务化搜索接口"""
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length)
        payload = json.loads(raw_body.decode("utf-8"))
        
        # 参数验证
        keywords = payload.get("keywords")
        if not keywords or not isinstance(keywords, list) or len(keywords) == 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "success": False,
                "detail": "keywords 必填，且必须是非空数组"
            })
            return
        
        generate_html = payload.get("generate_html")
        if generate_html is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "success": False,
                "detail": "generate_html 必填（true/false）"
            })
            return
        
        filters = payload.get("filters", [])
        platforms = payload.get("platforms", None)
        report_mode = payload.get("report_mode", "current")
        expand_keywords = payload.get("expand_keywords", True)
        
        # 执行搜索
        result = self._execute_search_task(
            keywords=keywords,
            filters=filters,
            platforms=platforms,
            report_mode=report_mode,
            expand_keywords=expand_keywords,
            generate_html=generate_html
        )
        
        # 返回结果
        if result["success"]:
            self._send_json(HTTPStatus.OK, result)
        else:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, result)
    
    def _handle_task_execute(self) -> None:
        """POST /api/tasks/{task_id}/execute - 执行任务"""
        if not HAS_DB:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "success": False,
                "detail": "数据库模块未初始化"
            })
            return
        
        # 从路径提取 task_id
        path_parts = self.path.split('/')
        task_id = path_parts[3] if len(path_parts) > 3 else None
        
        if not task_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "success": False,
                "detail": "缺少 task_id"
            })
            return
        
        try:
            db = TaskDatabase()
            task = db.get_task(task_id)
            
            if not task:
                self._send_json(HTTPStatus.NOT_FOUND, {
                    "success": False,
                    "detail": "任务不存在"
                })
                return
        except Exception as e:
            print(f"An error occurred: {e}")
            
    def _handle_task_execute(self) -> None:
        """POST /api/tasks/{task_id}/execute - 执行任务"""
        if not HAS_DB:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {
                "success": False,
                "detail": "数据库模块未初始化"
            })
            return
        
        # 从路径提取 task_id
        path_parts = self.path.split('/')
        task_id = path_parts[3] if len(path_parts) > 3 else None
        
        if not task_id:
            self._send_json(HTTPStatus.BAD_REQUEST, {
                "success": False,
                "detail": "缺少 task_id"
            })
            return
        
        try:
            db = TaskDatabase()
            task = db.get_task(task_id)
            
            if not task:
                self._send_json(HTTPStatus.NOT_FOUND, {
                    "success": False,
                    "detail": "任务不存在"
                })
                return
            
            # 使用任务配置执行搜索
            task_dict = task.to_dict()
            
            print(f"[任务执行] 开始执行任务: {task.name} ({task_id})")
            
            result = self._execute_search_task(
                keywords=task_dict["keywords"],
                filters=task_dict["filters"] or [],
                platforms=task_dict["platforms"] or [],
                report_mode=task_dict["report_mode"],
                expand_keywords=task_dict["expand_keywords"],
                generate_html=True,
                task_id=task_id  # 传入task_id用于记录历史
            )
            
            # 添加任务信息到返回结果
            result["task"] = {
                "id": task.id,
                "name": task.name
            }
            
            # 返回结果
            if result["success"]:
                self._send_json(HTTPStatus.OK, result)
            else:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, result)
                
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                "success": False,
                "detail": f"执行失败: {str(e)}"
            })


def main() -> None:
    server = HTTPServer(("0.0.0.0", 8090), ConfigRequestHandler)
    print("TrendRadar 配置中心已启动: http://localhost:8090")
    server.serve_forever()


if __name__ == "__main__":
    main()
