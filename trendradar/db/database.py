# coding=utf-8
"""
数据库操作类

trendradar/db/database.py
"""

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional
import json

from .models import User, Task, TaskExecution


class TaskDatabase:
    """任务数据库管理器"""
    
    def __init__(self, db_path: str = "output/tasks.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _get_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row  # 返回字典格式
        return conn
    
    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 用户表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                email TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        ''')
        
        # 任务表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                user_id TEXT NOT NULL,
                keywords TEXT NOT NULL,
                filters TEXT,
                platforms TEXT,
                report_mode TEXT DEFAULT 'current',
                schedule TEXT,
                expand_keywords INTEGER DEFAULT 1,
                status TEXT DEFAULT 'active',
                description TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        ''')
        
        # 任务执行历史表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                html_path TEXT,
                matched_count INTEGER DEFAULT 0,
                duration_ms INTEGER DEFAULT 0,
                status TEXT DEFAULT 'success',
                error_message TEXT,
                executed_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (task_id) REFERENCES tasks(id)
            )
        ''')
        
        # 创建索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_executions_task ON task_executions(task_id)')
        
        conn.commit()
        conn.close()
    
    # === 用户管理 ===
    
    def create_user(self, user_id: str, username: str, email: str = None) -> User:
        """创建或获取用户"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        try:
            # 尝试插入新用户
            cursor.execute(
                '''INSERT INTO users (id, username, email) VALUES (?, ?, ?)''',
                (user_id, username, email)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # 用户已存在，获取现有用户
            pass
        
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return User(**dict(row))
        return None
    
    def get_user(self, user_id: str) -> Optional[User]:
        """获取用户"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return User(**dict(row))
        return None
    
    def get_or_create_user(self, user_id: str, username: str = None, email: str = None) -> User:
        """获取或创建用户"""
        user = self.get_user(user_id)
        if user:
            return user
        
        # 如果没有提供用户名，使用 user_id
        if not username:
            username = user_id
        
        return self.create_user(user_id, username, email)
    
    # === 任务管理 ===
    
    def create_task(self, task: Task) -> Task:
        """创建任务"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 生成任务ID（如果没有）
        if not task.id:
            task.id = f"task_{uuid.uuid4().hex[:12]}"
        
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task.created_at = now
        task.updated_at = now
        
        cursor.execute('''
            INSERT INTO tasks (
                id, name, user_id, keywords, filters, platforms,
                report_mode, schedule, expand_keywords, status, description,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.id, task.name, task.user_id, task.keywords, task.filters,
            task.platforms, task.report_mode, task.schedule,
            int(task.expand_keywords), task.status, task.description,
            task.created_at, task.updated_at
        ))
        
        conn.commit()
        conn.close()
        
        return task
    
    def get_task(self, task_id: str) -> Optional[Task]:
        """获取任务"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return Task(**dict(row))
        return None
    
    def get_user_tasks(self, user_id: str, status: str = None) -> List[Task]:
        """获取用户的所有任务"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        if status:
            cursor.execute(
                'SELECT * FROM tasks WHERE user_id = ? AND status = ? ORDER BY created_at DESC',
                (user_id, status)
            )
        else:
            cursor.execute(
                'SELECT * FROM tasks WHERE user_id = ? ORDER BY created_at DESC',
                (user_id,)
            )
        
        rows = cursor.fetchall()
        conn.close()
        
        return [Task(**dict(row)) for row in rows]
    
    def update_task(self, task_id: str, updates: dict) -> bool:
        """更新任务"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 构建更新语句
        set_clauses = []
        values = []
        
        for key, value in updates.items():
            if key in ['keywords', 'filters', 'platforms']:
                # JSON 字段
                if isinstance(value, list):
                    value = json.dumps(value, ensure_ascii=False)
            elif key == 'expand_keywords':
                value = int(value)
            
            set_clauses.append(f"{key} = ?")
            values.append(value)
        
        # 添加 updated_at
        set_clauses.append("updated_at = ?")
        values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        values.append(task_id)
        
        sql = f"UPDATE tasks SET {', '.join(set_clauses)} WHERE id = ?"
        cursor.execute(sql, values)
        
        affected = cursor.rowcount
        conn.commit()
        conn.close()
        
        return affected > 0
    
    def delete_task(self, task_id: str) -> bool:
        """删除任务"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        affected = cursor.rowcount
        
        conn.commit()
        conn.close()
        
        return affected > 0
    
    # === 任务执行历史 ===
    
    def add_execution(self, execution: TaskExecution) -> int:
        """添加执行记录"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        if not execution.executed_at:
            execution.executed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        cursor.execute('''
            INSERT INTO task_executions (
                task_id, html_path, matched_count, duration_ms,
                status, error_message, executed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            execution.task_id, execution.html_path, execution.matched_count,
            execution.duration_ms, execution.status, execution.error_message,
            execution.executed_at
        ))
        
        execution_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        return execution_id
    
    def get_task_executions(self, task_id: str, limit: int = 10) -> List[TaskExecution]:
        """获取任务执行历史"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM task_executions 
            WHERE task_id = ? 
            ORDER BY executed_at DESC 
            LIMIT ?
        ''', (task_id, limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [TaskExecution(**dict(row)) for row in rows]
    
    def get_latest_execution(self, task_id: str) -> Optional[TaskExecution]:
        """获取最新执行记录"""
        executions = self.get_task_executions(task_id, limit=1)
        return executions[0] if executions else None
