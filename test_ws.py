import asyncio
import websockets

async def test():
    uri = "ws://localhost:8000/api/ws/simulator?days=30&top=50"
    async with websockets.connect(uri) as websocket:
        while True:
            try:
                message = await websocket.recv()
                print(f"< {message}")
            except websockets.ConnectionClosed:
                print("Connection closed by server")
                break

asyncio.run(test())
