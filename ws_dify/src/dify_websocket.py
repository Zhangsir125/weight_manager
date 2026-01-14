import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import uvicorn
import httpx
import asyncio
from uvicorn.config import LOGGING_CONFIG  # ✅ 新增：导入Uvicorn默认日志配置

app = FastAPI()

# 配置心跳参数 - 修复问题2：大小写统一为小写ping，和客户端一致
HEARTBEAT_INTERVAL = 20  # 心跳间隔：20秒发一次心跳包
HEARTBEAT_PING = "ping"  # 心跳请求标识 小写 ✅ 匹配客户端{"type": "ping"}
HEARTBEAT_PONG = "PONG"  # 心跳响应标识（保持连接的核心标识）
CONNECT_SUCCESS = "CONNECT_SUCCESS"  # 连接成功的标识


# ✅ 核心新增：带时间戳的通用日志打印函数
def log_with_time(msg):
    """打印带时间戳的日志，格式：[YYYY-MM-DD HH:MM:SS] 日志内容"""
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{current_time}] {msg}")


def unicode_escape_to_chinese(escape_str):
    try:
        # 处理不同格式的转义字符串（单反斜杠/双反斜杠）
        if isinstance(escape_str, str):
            # 确保最终编码是 utf-8，避免乱码
            return escape_str.encode('raw_unicode_escape').decode('unicode_escape')
        else:
            return "输入内容不是字符串格式！"
    except Exception as e:
        return f"转换失败：{str(e)}"


# ✅ 核心修改：给Uvicorn日志添加时间戳
# 1. 修改默认日志格式（包含时间）
LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(asctime)s - %(levelprefix)s %(message)s"
# 2. 修改访问日志格式（包含时间）
LOGGING_CONFIG["formatters"]["access"]["fmt"] = "%(asctime)s - %(levelprefix)s %(client_addr)s - \"%(request_line)s\" %(status_code)s"
# 3. 可选：自定义时间格式（默认是 %Y-%m-%d %H:%M:%S,%f，可简化）
LOGGING_CONFIG["formatters"]["default"]["datefmt"] = "%Y-%m-%d %H:%M:%S"
LOGGING_CONFIG["formatters"]["access"]["datefmt"] = "%Y-%m-%d %H:%M:%S"


# ✅ 安全发送消息的通用函数（封装状态判断，避免重复写）
async def safe_send_text(websocket, msg):
    """安全发送文本消息，仅当连接存活时发送"""
    if websocket.client_state.CONNECTED:
        await websocket.send_text(msg)


@app.websocket("/ws/dify")
async def websocket_endpoint(websocket: WebSocket):
    # ✅ 新增：连接关闭开关（核心，标记后心跳任务立即停止）
    is_closed = asyncio.Event()
    heartbeat_task = None
    try:
        # 1. WebSocket握手建立连接，必须第一行执行，正确无误
        await websocket.accept()
        await safe_send_text(websocket, f"[STATUS] {CONNECT_SUCCESS}")
        log_with_time("✅ 客户端WebSocket连接成功，已发送连接成功标识")

        # 定义心跳任务：定时给客户端发心跳响应，维持连接
        async def heartbeat():
            while True:
                # ✅ 优先判断关闭开关，一旦标记立即终止（比CONNECTED更优先）
                if is_closed.is_set():
                    log_with_time("[INFO] 心跳任务：检测到关闭开关，终止循环")
                    break
                # ✅ 核心：连接断开则立即终止心跳任务
                if not websocket.client_state.CONNECTED:
                    log_with_time("[INFO] 心跳任务检测到连接已关闭，终止心跳")
                    break
                try:
                    await safe_send_text(websocket, f"[HEARTBEAT] {HEARTBEAT_PONG}")
                    await asyncio.sleep(HEARTBEAT_INTERVAL)
                except Exception as e:
                    log_with_time(f"[WARNING] 心跳任务发送失败：{str(e)}")
                    break

        # 启动心跳后台任务，不阻塞正常消息收发
        heartbeat_task = asyncio.create_task(heartbeat())
        log_with_time("[INFO] 心跳任务启动")

        while True:
            try:
                # 接收客户端的JSON格式消息（心跳/业务请求）
                json_data = await asyncio.wait_for(websocket.receive_json(), timeout=35)
            except asyncio.TimeoutError:
                # 超时无消息，继续循环，心跳正常推送
                continue
            # 修复问题8：新增捕获【JSON格式错误】异常，友好提示，不会断开连接
            except Exception as e:
                await safe_send_text(websocket, "[ERROR] 请发送标准的JSON格式数据！")
                continue

            # ========== 心跳逻辑 ==========
            if json_data.get("type") == HEARTBEAT_PING:
                await safe_send_text(websocket, f"[HEARTBEAT] {HEARTBEAT_PONG}")
                log_with_time("📌 收到客户端心跳包，已回复PONG心跳标识")

            # ========== 正常业务请求逻辑 ==========
            else:
                # 统一获取参数+赋值正确的默认值+变量命名规范
                resm = json_data.get("resm", "")  # Dify接口后缀 如：chat-messages
                headers = json_data.get("headers", {})  # 请求头 默认空字典 ✅ 修复问题5
                row_data = json_data.get("data", {})  # 请求体参数 默认空字典

                # 基础参数校验，防止无效请求
                if not resm or not row_data:
                    await safe_send_text(websocket, "[ERROR] 参数错误：resm(接口后缀)和data(请求体)不能为空！")
                    log_with_time(f"[WARNING] 客户端参数错误：resm={resm}，data={row_data}")
                    continue

                # 发送请求中状态
                await safe_send_text(websocket, f"[STATUS] 正在请求AI回答，请稍候...")

                # Dify基础地址
                base_url = "https://myaitest.miyingbl.com/v1/"
                try:
                    # 异步调用Dify接口 - 修复问题3：必须加 stream=True 开启流式 ✅ 核心！
                    async with httpx.AsyncClient(timeout=60) as client:
                        async with client.stream(
                                method="POST",
                                url=base_url + resm,
                                headers=headers,
                                json=row_data
                        ) as response:
                            # 异步迭代Dify的流式返回数据
                            async for line in response.aiter_lines():
                                if line and line.startswith("data:") and "[DONE]" not in line:
                                    # 解码容错，防止特殊字符导致崩溃
                                    await safe_send_text(websocket, f"[AI_ANSWER] {line}")
                                else:
                                    if line:
                                        # await safe_send_text(websocket, f"[AI_ANSWER] data: {unicode_escape_to_chinese(line)}")
                                        await safe_send_text(websocket, f"[AI_ANSWER] data: {line}")
                    # AI回答推送完成
                    await safe_send_text(websocket, f"[STATUS] AI回答流式推送完成 ✔️")
                except Exception as req_err:
                    # 捕获请求Dify的异常，友好提示
                    err_msg = f"[ERROR] 请求Dify接口失败：{str(req_err)}"
                    await safe_send_text(websocket, err_msg)
                    log_with_time(err_msg)

    # 捕获客户端主动断开连接
    except WebSocketDisconnect:
        log_with_time("❌ 客户端主动断开WebSocket连接")
        # ✅ 立即标记关闭开关，阻断所有发送操作
        is_closed.set()
    # 捕获其他所有异常
    except Exception as e:
        err_info = f"[ERROR] 服务端异常：{str(e)}"
        log_with_time(err_info)
        is_closed.set()  # ✅ 标记关闭
        await safe_send_text(websocket, err_info)
    # 最终收尾：关闭连接+取消心跳任务
    finally:
        # 标记关闭开关（双重保险）
        is_closed.set()
        # 终止心跳任务（避免异步残留）
        if heartbeat_task and not heartbeat_task.done():
            try:
                heartbeat_task.cancel()
                await heartbeat_task  # 等待任务终止
                log_with_time("[INFO] 心跳任务已终止")
            except asyncio.CancelledError:
                log_with_time("[INFO] 心跳任务正常取消")
            except Exception as e:
                log_with_time(f"[WARNING] 终止心跳任务失败：{str(e)}")
        # 仅当连接存活时关闭（避免重复关闭）
        if websocket.client_state.CONNECTED:
            try:
                await websocket.close()
                log_with_time("🔚 连接已正常关闭")
            except Exception as e:
                # ✅ 过滤掉「关闭后发送」的无效报错，仅记录其他异常
                if "Cannot call 'send' once a close message has been sent" not in str(e):
                    log_with_time(f"[WARNING] 关闭连接失败：{str(e)}")
                else:
                    log_with_time("[INFO] 连接已关闭，忽略发送报错")
        else:
            log_with_time("🔚 连接已关闭，无需重复操作")


if __name__ == "__main__":
    log_with_time("🚀 服务启动中：0.0.0.0:8000")
    uvicorn.run(
        app="dify_websocket:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_config=LOGGING_CONFIG  # ✅ 关键：将修改后的日志配置传给Uvicorn
    )
