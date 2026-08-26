import os
import asyncio
from dotenv import load_dotenv
from browser_use import Agent, ChatOpenAI

load_dotenv()

llm = ChatOpenAI(
    model="~deepseek/deepseek-v4-flash-latest",
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
)

async def main():
    agent = Agent(
        task=(
            "find me the price of McDouble in Jacksonville area"
        ),
        llm=llm,
        use_vision=True,  # True = uses screenshots too, more tokens/cost, sometimes more accurate
    )
    result = await agent.run(max_steps=20)
    print("\n=== FINAL RESULT ===")
    print(result)

if __name__ == "__main__":
    asyncio.run(main())