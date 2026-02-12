# TrendRadar 服务化API使用文档

## 概述

TrendRadar 现在提供了一个统一的服务化API接口，可以通过HTTP请求执行完整的新闻抓取、分析、生成流程。

---

## API 接口

### `POST /api/search`

执行参数化搜索，支持返回HTML报告或链接列表。

---

## 请求参数

### 必填参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `keywords` | `string[]` | 关键词列表，至少包含一个关键词 |
| `generate_html` | `boolean` | 是否生成HTML报告（true=返回HTML路径，false=返回链接列表） |

### 可选参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `filters` | `string[]` | `[]` | 过滤词列表，包含这些词的新闻会被过滤掉 |
| `platforms` | `string[]` | 全部平台 | 指定要抓取的平台ID列表 |
| `report_mode` | `string` | `"current"` | 报告模式：`daily`（当日汇总）/ `current`（当前榜单）/ `incremental`（增量） |
| `user_id` | `string` | `null` | 用户ID（预留，用于后续多用户支持） |

---

## 响应格式

### 当 `generate_html=true` 时

返回HTML报告路径：

```json
{
  "success": true,
  "html_url": "output/html/2026-02-11/15-30.html",
  "html_path": "D:\\TRNNew\\output\\html\\2026-02-11\\15-30.html",
  "duration_ms": 45000,
  "stats": {
    "total_news": 150,
    "matched_keywords": 3,
    "platforms_count": 9
  }
}
```

**字段说明：**
- `html_url`: 相对路径，可以通过HTTP访问（如果配置了静态文件服务）
- `html_path`: 绝对路径，本地文件系统路径
- `duration_ms`: 执行耗时（毫秒）
- `stats.total_news`: 抓取的总新闻数
- `stats.matched_keywords`: 匹配到的关键词组数
- `stats.platforms_count`: 搜索的平台数量

### 当 `generate_html=false` 时

返回链接列表：

```json
{
  "success": true,
  "links": [
    {
      "title": "华为Mate 70系列发布",
      "url": "https://weibo.com/...",
      "mobile_url": "https://m.weibo.com/...",
      "platform": "微博",
      "ranks": [1, 3, 5],
      "keyword": "华为",
      "time_display": "09:00-12:00"
    }
  ],
  "duration_ms": 30000,
  "stats": {
    "total_news": 150,
    "matched_keywords": 3,
    "matched_links": 25,
    "platforms_count": 9
  }
}
```

**字段说明：**
- `links`: 匹配的新闻列表
  - `title`: 新闻标题
  - `url`: 桌面版链接
  - `mobile_url`: 移动版链接
  - `platform`: 来源平台
  - `ranks`: 在该平台的排名历史
  - `keyword`: 匹配的关键词
  - `time_display`: 出现的时间段
- `stats.matched_links`: 匹配的链接总数

---

## 使用示例

### 示例1：搜索华为和苹果的新闻，生成HTML报告

**请求：**
```bash
curl -X POST http://localhost:8090/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "keywords": ["华为", "苹果"],
    "filters": ["广告", "抽奖"],
    "generate_html": true,
    "report_mode": "current"
  }'
```

**响应：**
```json
{
  "success": true,
  "html_url": "output/html/2026-02-11/15-30.html",
  "html_path": "D:\\TRNNew\\output\\html\\2026-02-11\\15-30.html",
  "duration_ms": 42000,
  "stats": {
    "total_news": 180,
    "matched_keywords": 2,
    "platforms_count": 9
  }
}
```

访问报告：打开浏览器访问 `file:///D:/TRNNew/output/html/2026-02-11/15-30.html`

---

### 示例2：只搜索微博和知乎，返回链接列表

**请求：**
```bash
curl -X POST http://localhost:8090/api/search \
  -H "Content-Type: application/json" \
  -d '{
    "keywords": ["AI", "人工智能"],
    "platforms": ["weibo", "zhihu"],
    "generate_html": false
  }'
```

**响应：**
```json
{
  "success": true,
  "links": [
    {
      "title": "OpenAI发布最新AI模型",
      "url": "https://weibo.com/...",
      "platform": "微博",
      "ranks": [2, 5],
      "keyword": "AI"
    },
    {
      "title": "人工智能如何改变生活",
      "url": "https://www.zhihu.com/question/...",
      "platform": "知乎",
      "ranks": [10],
      "keyword": "人工智能"
    }
  ],
  "duration_ms": 15000,
  "stats": {
    "total_news": 50,
    "matched_keywords": 2,
    "matched_links": 2,
    "platforms_count": 2
  }
}
```

---

### 示例3：Python调用示例

```python
import requests
import json

# API地址
api_url = "http://localhost:8090/api/search"

# 请求参数
payload = {
    "keywords": ["华为", "苹果"],
    "filters": ["广告"],
    "generate_html": True,
    "report_mode": "current"
}

# 发送请求
response = requests.post(api_url, json=payload)
result = response.json()

if result["success"]:
    if "html_path" in result:
        print(f"✅ HTML报告已生成: {result['html_path']}")
        print(f"⏱️  耗时: {result['duration_ms']}ms")
    else:
        print(f"✅ 找到 {len(result['links'])} 条新闻")
        for link in result["links"][:5]:  # 只显示前5条
            print(f"  - {link['title']} ({link['platform']})")
else:
    print(f"❌ 搜索失败: {result['detail']}")
```

---

### 示例4：JavaScript/Node.js调用示例

```javascript
const fetch = require('node-fetch');

async function searchNews() {
  const response = await fetch('http://localhost:8090/api/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      keywords: ['华为', '苹果'],
      filters: ['广告'],
      generate_html: false
    })
  });

  const result = await response.json();

  if (result.success) {
    console.log(`✅ 找到 ${result.stats.matched_links} 条新闻`);
    result.links.forEach(link => {
      console.log(`- ${link.title} (${link.platform})`);
    });
  } else {
    console.error(`❌ 搜索失败: ${result.detail}`);
  }
}

searchNews();
```

---

## 平台ID列表

根据你的 `config.yaml` 配置，可用的平台ID：

| 平台ID | 平台名称 |
|--------|----------|
| `toutiao` | 今日头条 |
| `baidu` | 百度热搜 |
| `thepaper` | 澎湃新闻 |
| `weibo` | 微博 |
| `douyin` | 抖音 |
| `zhihu` | 知乎 |
| `freebuf` | Freebuf |
| `juejin` | 稀土掘金 |
| `nowcoder` | 牛客 |

---

## 报告模式说明

### `daily`（当日汇总模式）
- 显示当天所有匹配的新闻
- 适合：日报总结、全面了解当日热点

### `current`（当前榜单模式）**（默认）**
- 只显示当前在榜的新闻
- 适合：实时热点追踪

### `incremental`（增量监控模式）
- 只显示新增的新闻
- 适合：避免重复信息

---

## 错误处理

### 错误响应格式

```json
{
  "success": false,
  "detail": "错误信息"
}
```

### 常见错误

| HTTP状态码 | 错误原因 | 解决方法 |
|-----------|---------|---------|
| `400` | 缺少必填参数 | 检查 `keywords` 和 `generate_html` 参数 |
| `500` | 服务器内部错误 | 查看服务器日志，可能是配置问题或模块导入失败 |

---

## 性能说明

### 预计耗时

- **搜索所有平台（9个）+ RSS（6个）**：约 30-40 秒
- **只搜索指定平台（2-3个）**：约 10-15 秒
- **生成HTML**：额外 1-2 秒

### 优化建议

1. **限制平台数量**：使用 `platforms` 参数只搜索需要的平台
2. **并发请求**：如果有多个查询，可以并发发送（服务端有互斥锁，会排队执行）
3. **缓存结果**：对于相同的关键词，可以在客户端缓存结果

---

## 后续集成建议

### 集成到登录系统

当你把这个项目集成到有登录系统的应用时，可以这样做：

1. **用户认证**：在请求中传递 `user_id` 参数
2. **权限控制**：在服务端验证 `user_id` 的合法性
3. **结果存储**：根据 `user_id` 保存用户的搜索历史
4. **订阅管理**：结合第2个任务（任务ID+用户ID），实现订阅功能

**示例：**
```json
{
  "user_id": "user123",
  "keywords": ["华为"],
  "generate_html": true,
  "save_as_subscription": true  // 保存为订阅（需要后续实现）
}
```

---

## 总结

✅ 现在你已经有了一个**完整的服务化API**！

**核心特性：**
- ✅ 参数化搜索（关键词+过滤词）
- ✅ 灵活的返回格式（HTML报告 或 链接列表）
- ✅ 支持平台选择
- ✅ 支持多种报告模式
- ✅ 完整的错误处理

**下一步：**
- 第2个任务：添加用户-任务多对多关系
- 添加用户认证和权限控制
- 部署到服务器，提供公网访问
