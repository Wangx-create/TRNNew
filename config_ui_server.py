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

ROOT_DIR = Path(__file__).resolve().parent
CONFIG_DIR = ROOT_DIR / "config"
FREQUENCY_FILE = CONFIG_DIR / "frequency_words.txt"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
TIMELINE_FILE = CONFIG_DIR / "timeline.yaml"
SCHEDULE_FILE = CONFIG_DIR / "local_schedule.json"
UI_DIR = ROOT_DIR / "config_ui"
_RUN_LOCK = threading.Lock()

# --- 修改后的 Prompt ---
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
        self.send_error(HTTPStatus.NOT_FOUND, "未找到资源")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/suggest":
            self._handle_suggest()
            return
        if parsed.path == "/api/run":
            self._handle_run()
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
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            data = resp.json()
            # 直接提取 AI 的完整回复内容，不再做单行正则提取
            content = data["choices"][0]["message"]["content"].strip()
            self._send_json(HTTPStatus.OK, {"regex": content})
        except Exception as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"detail": str(exc)})


def main() -> None:
    server = HTTPServer(("0.0.0.0", 8090), ConfigRequestHandler)
    print("TrendRadar 配置中心已启动: http://localhost:8090")
    server.serve_forever()


if __name__ == "__main__":
    main()
