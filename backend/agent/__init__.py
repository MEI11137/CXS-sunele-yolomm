"""内部 Agent 运行时（WB17-02）。

模块分工：

* ``engine``   —— OpenCode 子进程适配器（真实模型循环、规划、工具调用）
* ``gateway``  —— 私有回环工具网关（令牌换身份、权限模式、动作确认、幂等）
* ``mcp_bridge`` —— 引擎侧 stdio MCP 桥（转发到网关）
* ``tools``    —— 工具清单与参数 schema
* ``fsops``    —— 绑定目录内的受控文件操作
* ``runner``   —— 后台轮次执行器（事件、停止、最终回答判定）
* ``prompt`` / ``policy`` / ``identity`` / ``store`` —— 提示、权限、指纹、持久化
"""
