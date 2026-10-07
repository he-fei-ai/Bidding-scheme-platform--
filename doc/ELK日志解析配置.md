# ELK 日志解析配置
# ========================
# 本目录包含用于解析招投标方案平台后端日志的 ELK/Kibana 配置
# 日志来源: logs/backend.log
#
# 对应 ELK 版本: Elasticsearch 8.x / Logstash 8.x / Kibana 8.x

# 一、Logstash Pipeline 配置
# 文件: logstash/pipeline/outline-logs.conf
# 使用说明: 将此文件放入 Logstash 的 pipeline/ 目录

"""
input {
  file {
    path => "J:/编程/招投标方案平台/logs/backend.log"
    start_position => "beginning"
    sincedb_path => "NUL"
    codec => json {
      # 如果日志格式为: timestamp [level] logger: LOG_JSON: {...}
      # 则需要先过滤，再用 multiline 或 filter 解析
    }
    # 避免已读取的文件重复发送
    sincedb_write_interval => 30
  }
}

filter {
  # 只处理 LOG_JSON 格式的日志行
  if [message] =~ "LOG_JSON:" {
    # 提取 LOG_JSON 后面的 JSON 内容
    grok {
      match => {
        "message" => "%{TIMESTAMP_ISO8601:log_timestamp}\s+\[%{LOGLEVEL:log_level}\]\s+%{DATA:log_logger}:\s+LOG_JSON:\s+%{GREEDYDATA:log_json_data}"
      }
    }
    # 解析提取的 JSON 数据
    json {
      source => "log_json_data"
      target => "log_event"
    }
    # 使用事件时间戳作为 Elasticsearch 写入时间
    date {
      match => ["log_event[timestamp]", "ISO8601"]
      target => "@timestamp"
    }
    # 添加平台标识
    mutate {
      add_field => { "platform" => "招投标方案平台" }
      add_field => { "log_source" => "backend" }
    }
  }

  # 普通日志行处理
  grok {
    match => {
      "message" => "%{TIMESTAMP_ISO8601:log_timestamp}\s+\[%{LOGLEVEL:log_level}\]\s+%{DATA:log_logger}:\s+%{GREEDYDATA:log_message}"
    }
  }
  date {
    match => ["log_timestamp", "ISO8601"]
    target => "@timestamp"
  }
}

output {
  elasticsearch {
    hosts => ["http://localhost:9200"]
    index => "outline-logs-%{+YYYY.MM.dd}"
    # 按 event_id 去重（同一事件不会重复写入）
    document_id => "%{[log_event][event_id]}"
  }
  # 调试输出（可选）
  stdout { codec => rubydebug }
}
"""

# 二、Elasticsearch Index Pattern
# Kibana → Stack Management → Index Patterns → Create index pattern
# Index pattern: outline-logs-*
# Time field: @timestamp

# 三、Kibana Dev Tools 查询示例
# GET outline-logs-*/_search
# {
#   "size": 20,
#   "sort": [{ "@timestamp": { "order": "desc" } }],
#   "query": {
#     "match": { "log_event.event_type": "outline_phase" }
#   },
#   "_source": ["log_event.event_id", "log_event.phase", "log_event.elapsed_s", "log_event.timestamp"]
# }

# 四、关键日志字段说明
# event_type  = "outline_phase"  | 流程阶段事件
# event_id    = "step3_merge_start" | 事件唯一标识
# phase       = "步骤3: 智能合并目录开始" | 可读阶段描述
# elapsed_s   = 0.0            | 本阶段耗时（秒）
# total_elapsed_s = 1.23       | 累计总耗时（秒）
# timestamp   = ISO8601        | UTC 时间戳
#
# 事件 ID 全集:
#   step3_merge_start       | 步骤3：智能合并目录开始
#   step3_merge_complete    | 步骤3：智能合并目录完成
#   step4_sort_start        | 步骤4：V6.0 智能排序开始
#   step4_sort_complete     | 步骤4：V6.0 智能排序完成
#   step5_distribution      | 步骤5：评分项对齐分发
#   fill_children_prepare   | 步骤5：章节填充准备
#   phase1_serial_complete  | Phase1：串行章节填充完成
#   phase3_parallel_start   | Phase3：并行填充启动
#   phase3_parallel_complete| Phase3：并行填充完成
#   v36_analysis_start      | V3.6：并发分析启动
#   v36_analysis_complete   | V3.6：并发分析完成
#   validation_result       | 目录业务校验结果
#   overall_complete        | 目录生成整体完成

# 五、Kibana Dashboard 推荐配置
# 1. 时间序列图：按 elapsed_s 追踪各阶段耗时
# 2. 柱状图：按 event_id 统计各阶段触发次数
# 3. 表格：展示 phase、elapsed_s、total_elapsed_s
# 4. 告警规则：total_elapsed_s > 30s 时触发告警
