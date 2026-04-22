import asyncio
from hermes.config import MINIMAX_API_KEY
print('Key length:', len(MINIMAX_API_KEY), 'Starts with quote:', MINIMAX_API_KEY.startswith('"'))

async def check():
    from hermes.agent.agent import HermesAgent
    agent = HermesAgent()
    try:
        res = await agent.chat('test', 'hi')
        print('SUCCESS! Response length:', len(res))
    except Exception as e:
        print('FAILED:', e)

asyncio.run(check())
