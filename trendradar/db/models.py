# coding=utf-8
"""
数据库模型定义

trendradar/db/models.py
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List
import json


@dataclass
class User:
    """用户模型"""
    id: str                      # user_123
    username: str
    email: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }


@dataclass
class Task:
    """任务模型"""
    id: str                           # task_abc123
    name: str
    user_id: str                      # 所属用户
    keywords: str                     # JSON: ["华为", "苹果"]
    filters: Optional[str] = None     # JSON: ["广告"]
    platforms: Optional[str] = None   # JSON: ["weibo", "zhihu"]
    report_mode: str = "current"
    schedule: Optional[str] = None
    expand_keywords: bool = True
    status: str = "active"            # active/paused/archived
    description: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    
    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "user_id": self.user_id,
            "keywords": json.loads(self.keywords) if isinstance(self.keywords, str) else self.keywords,
            "filters": json.loads(self.filters) if isinstance(self.filters, str) else self.filters,
            "platforms": json.loads(self.platforms) if isinstance(self.platforms, str) else self.platforms,
            "report_mode": self.report_mode,
            "schedule": self.schedule,
            "expand_keywords": bool(self.expand_keywords),
            "status": self.status,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Task':
        """从字典创建任务对象"""
        return cls(
            id=data.get('id', ''),
            name=data['name'],
            user_id=data['user_id'],
            keywords=json.dumps(data['keywords'], ensure_ascii=False) if isinstance(data['keywords'], list) else data['keywords'],
            filters=json.dumps(data.get('filters', []), ensure_ascii=False) if isinstance(data.get('filters'), list) else data.get('filters'),
            platforms=json.dumps(data.get('platforms', []), ensure_ascii=False) if isinstance(data.get('platforms'), list) else data.get('platforms'),
            report_mode=data.get('report_mode', 'current'),
            schedule=data.get('schedule'),
            expand_keywords=data.get('expand_keywords', True),
            status=data.get('status', 'active'),
            description=data.get('description')
        )


@dataclass
class TaskExecution:
    """任务执行记录"""
    id: Optional[int] = None
    task_id: str = ""
    html_path: Optional[str] = None
    matched_count: int = 0
    duration_ms: int = 0
    status: str = "success"           # success/failed
    error_message: Optional[str] = None
    executed_at: Optional[str] = None
    
    def to_dict(self):
        return {
            "id": self.id,
            "task_id": self.task_id,
            "html_path": self.html_path,
            "matched_count": self.matched_count,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_message": self.error_message,
            "executed_at": self.executed_at
        }