# TrendRadar 任务管理系统 - 部署指南

## 📁 文件部署位置

### 1. 数据库模块文件

创建 `trendradar/db/` 目录，并放置以下文件：

```
TRNNew/
├── trendradar/
│   ├── db/
│   │   ├── __init__.py      ← db__init__.py 重命名为此
│   │   ├── models.py        ← models.py
│   │   └── database.py      ← database.py
│   └── ...
├── config_ui_server.py      ← 替换现有文件
└── output/
    └── tasks.db             ← 自动生成
```

### 2. 执行步骤

```bash
# 1. 创建数据库目录
cd D:\TRNNew
mkdir trendradar\db

# 2. 复制文件
# 将 models.py 复制到 trendradar\db\models.py
# 将 database.py 复制到 trendradar\db\database.py
# 将 db__init__.py 重命名并复制到 trendradar\db\__init__.py

# 3. 替换服务器文件
# 用新的 config_ui_server.py 替换根目录下的同名文件

# 4. 启动服务器
python config_ui_server.py
```

---

## 🎯 核心设计思路

### 多用户隔离机制

**问题：** 多个用户共用一套 `config.yaml` 和 `frequency_words.txt`

**解决方案：** 
- 每个任务执行时，临时覆盖配置文件
- 执行完成后，立即恢复原始配置
- 使用文件锁防止并发冲突

**执行流程：**
```
1. 任务开始执行
   ↓
2. 备份当前配置文件
   backup_config = 读取 config.yaml
   backup_freq = 读取 frequency_words.txt
   ↓
3. 写入任务专属配置
   写入任务的关键词 → frequency_words.txt
   写入任务的平台/模式 → config.yaml
   ↓
4. 运行 NewsAnalyzer
   (使用任务配置执行)
   ↓
5. 恢复原始配置
   写回 backup_config → config.yaml
   写回 backup_freq → frequency_words.txt
   ↓
6. 返回结果
```

---

## 📊 API 使用示例

### 1. 创建任务

```bash
curl -X POST http://localhost:8090/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "wangxin",
    "name": "科技新闻监控",
    "keywords": ["华为", "苹果"],
    "filters": ["广告"],
    "platforms": ["weibo", "zhihu"],
    "report_mode": "current",
    "expand_keywords": true,
    "description": "监控科技公司最新动态"
  }'
```

**响应：**
```json
{
  "success": true,
  "message": "任务已创建",
  "task": {
    "id": "task_a1b2c3d4e5f6",
    "name": "科技新闻监控",
    "user_id": "wangxin",
    "keywords": ["华为", "苹果"],
    "status": "active",
    "created_at": "2026-02-12 14:30:00"
  }
}
```

---

### 2. 获取我的任务列表

```bash
curl "http://localhost:8090/api/tasks?user_id=wangxin"
```

**响应：**
```json
{
  "success": true,
  "tasks": [
    {
      "id": "task_a1b2c3d4e5f6",
      "name": "科技新闻监控",
      "keywords": ["华为", "苹果"],
      "status": "active"
    }
  ],
  "total": 1
}
```

---

### 3. 更新任务

```bash
curl -X POST http://localhost:8090/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "wangxin",
    "task_id": "task_369749038703",
    "name": "科技新闻监控（更新）",
    "keywords": ["华为", "小米", "苹果"]
  }'
```

---

### 4. 立即执行任务

```bash
curl -X POST http://localhost:8090/api/tasks/task_369749038703/execute
```

**响应：**
```json
{
  "success": true,
  "html_url": "output/html/2026-02-12/14-35.html",
  "duration_ms": 35000,
  "task": {
    "id": "task_a1b2c3d4e5f6",
    "name": "科技新闻监控"
  }
}
```

---

### 5. 查看任务详情（包含执行历史）

```bash
curl "http://localhost:8090/api/tasks/task_a1b2c3d4e5f6"
```

**响应：**
```json
{
  "success": true,
  "task": {
    "id": "task_a1b2c3d4e5f6",
    "name": "科技新闻监控",
    "keywords": ["华为", "苹果"]
  },
  "executions": [
    {
      "id": 1,
      "task_id": "task_a1b2c3d4e5f6",
      "html_path": "output/html/2026-02-12/14-35.html",
      "duration_ms": 35000,
      "status": "success",
      "executed_at": "2026-02-12 14:35:00"
    }
  ]
}
```

---

## 🔄 完整使用流程

### 场景：用户从前端创建并执行任务

```javascript
// 1. 前端：用户填写表单
const taskData = {
  user_id: "wangxin",        // 从登录态获取
  name: "科技新闻监控",
  keywords: ["华为", "苹果"],
  filters: ["广告"],
  platforms: ["weibo", "zhihu"]
};

// 2. 创建任务
const createResp = await fetch('/api/tasks', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(taskData)
});

const {task} = await createResp.json();
console.log('任务已创建:', task.id);

// 3. 立即执行任务
const executeResp = await fetch(`/api/tasks/${task.id}/execute`, {
  method: 'POST'
});

const result = await executeResp.json();
console.log('HTML报告:', result.html_url);

// 4. 查看任务列表
const listResp = await fetch(`/api/tasks?user_id=wangxin`);
const {tasks} = await listResp.json();
console.log('我的任务:', tasks);
```

---

## 🔐 用户ID匹配逻辑

### 自动创建用户

```python
# 当接收到 user_id 时
db = TaskDatabase()
user = db.get_or_create_user(user_id)

# 如果用户不存在，自动创建
# 如果已存在，直接返回
```

### 任务权限检查

```python
# 更新任务时
existing_task = db.get_task(task_id)
if existing_task.user_id != user_id:
    return {"error": "无权限修改此任务"}
```

---

## 📝 Python 测试脚本

创建 `test_task_api.py`：

```python
#!/usr/bin/env python
# coding=utf-8
import requests
import json

API_BASE = "http://localhost:8090/api"
USER_ID = "wangxin"

def test_task_api():
    # 1. 创建任务
    print("=" * 60)
    print("1. 创建任务")
    print("=" * 60)
    
    create_resp = requests.post(f"{API_BASE}/tasks", json={
        "user_id": USER_ID,
        "name": "科技新闻监控",
        "keywords": ["华为", "苹果"],
        "filters": ["广告"],
        "platforms": ["weibo", "zhihu"],
        "expand_keywords": True
    })
    
    create_data = create_resp.json()
    print(json.dumps(create_data, indent=2, ensure_ascii=False))
    
    if not create_data.get("success"):
        print("创建失败！")
        return
    
    task_id = create_data["task"]["id"]
    print(f"\n✅ 任务已创建: {task_id}")
    
    # 2. 获取任务列表
    print("\n" + "=" * 60)
    print("2. 获取任务列表")
    print("=" * 60)
    
    list_resp = requests.get(f"{API_BASE}/tasks?user_id={USER_ID}")
    list_data = list_resp.json()
    
    print(f"任务总数: {list_data['total']}")
    for task in list_data['tasks']:
        print(f"  - {task['name']} ({task['id']})")
    
    # 3. 更新任务
    print("\n" + "=" * 60)
    print("3. 更新任务")
    print("=" * 60)
    
    update_resp = requests.post(f"{API_BASE}/tasks", json={
        "user_id": USER_ID,
        "task_id": task_id,
        "name": "科技新闻监控（已更新）",
        "keywords": ["华为", "小米", "苹果"]
    })
    
    update_data = update_resp.json()
    print(json.dumps(update_data, indent=2, ensure_ascii=False))
    
    # 4. 执行任务
    print("\n" + "=" * 60)
    print("4. 执行任务")
    print("=" * 60)
    
    print(f"正在执行任务 {task_id}，请等待...")
    
    execute_resp = requests.post(f"{API_BASE}/tasks/{task_id}/execute")
    execute_data = execute_resp.json()
    
    if execute_data.get("success"):
        print(f"✅ 执行成功！")
        print(f"HTML报告: {execute_data['html_url']}")
        print(f"耗时: {execute_data['duration_ms']}ms")
    else:
        print(f"❌ 执行失败: {execute_data.get('detail')}")
    
    # 5. 查看执行历史
    print("\n" + "=" * 60)
    print("5. 查看任务详情和执行历史")
    print("=" * 60)
    
    detail_resp = requests.get(f"{API_BASE}/tasks/{task_id}")
    detail_data = detail_resp.json()
    
    print(f"任务: {detail_data['task']['name']}")
    print(f"执行历史 ({len(detail_data['executions'])} 条):")
    for ex in detail_data['executions']:
        print(f"  - {ex['executed_at']}: {ex['status']} ({ex['duration_ms']}ms)")

if __name__ == "__main__":
    test_task_api()
```

**运行测试：**
```bash
python test_task_api.py
```

---

## ⚙️ 数据库说明

### 数据库位置
- 路径：`output/tasks.db`
- 类型：SQLite 3
- 自动创建（首次启动时）

### 查看数据库内容

**方法1：使用 SQLite 命令行**
```bash
sqlite3 output/tasks.db

# 查看所有表
.tables

# 查看用户
SELECT * FROM users;

# 查看任务
SELECT * FROM tasks;

# 查看执行历史
SELECT * FROM task_executions;
```

**方法2：使用 DB Browser for SQLite**
- 下载：https://sqlitebrowser.org/
- 打开 `output/tasks.db` 文件
- 可视化查看和编辑

---

## 🔧 常见问题

### Q1: 数据库模块导入失败

**错误信息：**
```
[警告] 数据库模块未找到，任务管理功能将不可用
```

**解决方案：**
```bash
# 检查文件是否正确放置
ls trendradar/db/
# 应该看到：__init__.py  database.py  models.py

# 检查 __init__.py 内容
cat trendradar/db/__init__.py
```

---

### Q2: 多个用户同时执行任务会冲突吗？

**不会！** 代码使用了备份-恢复机制：

```python
# 用户A执行
备份配置 → 写入用户A的配置 → 运行 → 恢复配置

# 用户B执行（即使在用户A执行期间）
备份配置 → 写入用户B的配置 → 运行 → 恢复配置
```

但为了保险，建议：
- 如果预期并发量大，添加执行队列
- 使用任务调度系统（如 Celery）

---

### Q3: 如何备份任务数据？

**方法1：直接复制数据库文件**
```bash
cp output/tasks.db output/tasks_backup_2026-02-12.db
```

**方法2：导出为SQL**
```bash
sqlite3 output/tasks.db .dump > tasks_backup.sql
```

---

### Q4: 如何清理旧的执行历史？

**手动清理：**
```bash
sqlite3 output/tasks.db

# 删除30天前的执行记录
DELETE FROM task_executions 
WHERE executed_at < datetime('now', '-30 days');
```

**自动清理（可选）：**
在 `database.py` 的 `add_execution` 方法中已经实现了保留最近100条的逻辑。

---

## 🎉 完成！

现在你的系统支持：

✅ **多用户隔离**：每个用户独立的任务配置  
✅ **任务管理**：创建、更新、删除、查询  
✅ **立即执行**：点击即可执行任务  
✅ **执行历史**：记录每次执行的结果  
✅ **自动匹配**：user_id 自动创建用户  
✅ **权限控制**：只能修改自己的任务  

---

## 📞 后续支持

如果需要添加：
1. 定时调度（按schedule字段自动执行）
2. 任务共享（多用户共用一个任务）
3. 权限分级（owner/editor/viewer）
4. 通知推送（任务完成后通知）

随时告诉我！🚀
