#!/usr/bin/env python
# coding=utf-8
"""
TrendRadar 服务化 API 测试脚本
"""

import argparse
import json
import requests
import time

def test_search_api(
    keywords=None,
    filters=None,
    generate_html=True,
    platforms=None,
    report_mode="current",
    expand_keywords=True,
    api_url="http://localhost:8090/api/search"
):
    """测试搜索API"""
    
    if keywords is None:
        keywords = ["华为", "苹果"]
    if filters is None:
        filters = ["震惊"]
    
    print("=" * 60)
    print("TrendRadar 服务化 API 测试")
    print("=" * 60)
    print(f"关键词: {', '.join(keywords)}")
    print(f"过滤词: {', '.join(filters)}")
    print(f"生成HTML: {generate_html}")
    print(f"AI扩展: {expand_keywords}")
    if platforms:
        print(f"指定平台: {', '.join(platforms)}")
    else:
        print(f"平台: 全部")
    print(f"报告模式: {report_mode}")
    print("=" * 60)
    
    # 构建请求
    payload = {
        "keywords": keywords,
        "filters": filters,
        "generate_html": generate_html,
        "report_mode": report_mode,
        "expand_keywords": expand_keywords
    }
    
    if platforms:
        payload["platforms"] = platforms
    
    # 发送请求
    print("\n正在发送请求...")
    start_time = time.time()
    
    try:
        response = requests.post(
            api_url,
            json=payload,
            timeout=120  # 2分钟超时
        )
        
        elapsed = time.time() - start_time
        
        # 解析响应
        result = response.json()
        
        if response.status_code == 200 and result.get("success"):
            print(f"请求成功！（客户端耗时: {elapsed:.1f}秒）\n")
            
            if generate_html:
                print("HTML 报告:")
                print(f"  - 相对路径: {result['html_url']}")
                print(f"  - 绝对路径: {result['html_path']}")
                print(f"  - 服务端耗时: {result['duration_ms']}ms")
                print(f"\n统计信息:")
                stats = result.get('stats', {})
                if 'total_news' in stats:
                    print(f"  - 总新闻数: {stats['total_news']}")
                if 'matched_keywords' in stats:
                    print(f"  - 匹配关键词: {stats['matched_keywords']}")
                if 'platforms_count' in stats:
                    print(f"  - 搜索平台: {stats['platforms_count']}")
                
                print(f"\n访问报告:")
                print(f"  file:///{result['html_path'].replace(chr(92), '/')}")
            else:
                print("链接列表:")
                links = result['links']
                print(f"  - 共找到 {len(links)} 条新闻")
                print(f"  - 服务端耗时: {result['duration_ms']}ms")
                print(f"\n统计信息:")
                stats = result.get('stats', {})
                if 'total_news' in stats:
                    print(f"  - 总新闻数: {stats['total_news']}")
                if 'matched_keywords' in stats:
                    print(f"  - 匹配关键词: {stats['matched_keywords']}")
                if 'matched_links' in stats:
                    print(f"  - 匹配链接: {stats['matched_links']}")
                if 'platforms_count' in stats:
                    print(f"  - 搜索平台: {stats['platforms_count']}")
                
                # 显示新闻链接
                print(f"\n新闻列表:")
                for i, link in enumerate(links, 1):
                    print(f"\n  {i}. {link['title']}")
                    print(f"     平台: {link['platform']}")
                    if link.get('ranks'):
                        print(f"     排名: {', '.join(map(str, link['ranks']))}")
                    print(f"     链接: {link['url']}")
        else:
            print(f"请求失败！")
            print(f"HTTP状态码: {response.status_code}")
            print(f"错误信息: {result.get('detail', '未知错误')}")
            
    except requests.Timeout:
        print(f"请求超时！（{elapsed:.1f}秒）")
    except requests.RequestException as e:
        print(f"请求异常: {e}")
    except json.JSONDecodeError:
        print(f"响应解析失败")
        print(f"原始响应: {response.text[:500]}")
    
    print("\n" + "=" * 60)


def main():
    parser = argparse.ArgumentParser(description="测试 TrendRadar 服务化API")
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=["华为", "苹果"],
        help="关键词列表（默认: 华为 苹果）"
    )
    parser.add_argument(
        "--filters",
        nargs="+",
        default=["震惊"],
        help="过滤词列表（默认: 震惊）"
    )
    parser.add_argument(
        "--links",
        action="store_true",
        help="返回链接列表（默认返回HTML）"
    )
    parser.add_argument(
        "--platforms",
        nargs="+",
        default=None,
        help="指定平台ID列表（默认: 全部平台）"
    )
    parser.add_argument(
        "--mode",
        choices=["daily", "current", "incremental"],
        default="current",
        help="报告模式（默认: current）"
    )
    parser.add_argument(
        "--no-expand",
        action="store_true",
        help="禁用AI关键词扩展（默认: 启用）"
    )
    parser.add_argument(
        "--url",
        default="http://localhost:8090/api/search",
        help="API地址（默认: http://localhost:8090/api/search）"
    )
    
    args = parser.parse_args()
    
    test_search_api(
        keywords=args.keywords,
        filters=args.filters,
        generate_html=not args.links,
        platforms=args.platforms,
        report_mode=args.mode,
        expand_keywords=not args.no_expand,
        api_url=args.url
    )


if __name__ == "__main__":
    main()