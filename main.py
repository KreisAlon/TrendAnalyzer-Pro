import asyncio
import sys
import os
import httpx
import textwrap
from datetime import datetime

# --- System Path Setup ---
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Internal Imports ---
import config
from database.manager import TrendManager
from collectors.github import GitHubCollector
from collectors.hacker_news import HackerNewsCollector
from collectors.mastodon import MastodonCollector
from collectors.devto import DevToCollector

# --- Configuration ---
REFRESH_INTERVAL_MINUTES = 60
REFRESH_INTERVAL_SECONDS = REFRESH_INTERVAL_MINUTES * 60


async def run_cycle(cycle_num, start_time):
    """
    Executes a single data collection cycle.
    
    Architecture Note:
    This cycle now implements full concurrency at the highest level. Instead of 
    waiting for each platform to finish its collection sequentially, it dispatches 
    all platform collectors simultaneously using asyncio.gather. It also handles 
    storage via lightweight JSON keywords and cross-platform normalization.
    """
    print(f"\n" + "=" * 80)
    print(f"🕒 CYCLE #{cycle_num} STARTING | TIME: {start_time}")
    print("=" * 80)

    # Initialize Database Manager
    db_manager = TrendManager()

    # Initialize Data Collectors
    collectors = [
        GitHubCollector(),
        HackerNewsCollector(),
        MastodonCollector(),
        DevToCollector()
    ]

    all_posts = []

    try:
        # Using a shared async client with connection pooling for maximum network efficiency
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                     headers={'User-Agent': 'TrendAnalyzer/5.0'}) as client:
            
            # 1. Ingest Data from all platforms CONCURRENTLY
            print(">>> 🚀 Launching all platform collectors simultaneously...")
            
            # Create a list of awaitable tasks, one for each platform's main collect() method
            collection_tasks = [collector.collect(client) for collector in collectors]
            
            # Execute all high-level platform tasks concurrently. 
            # Total execution time is now bounded by the slowest platform, not the sum of all.
            platform_results = await asyncio.gather(*collection_tasks)
            
            # Flatten the list of lists returned by gather() into a single 1D array of posts
            for platform_posts in platform_results:
                all_posts.extend(platform_posts)

            # 2. Save new unique items (Embeddings removed, now using lightweight keywords)
            new_count = db_manager.save_posts(all_posts)
            print(f">>> 💾 Saved {new_count} new unique items to the database.")

            # 3. Trigger statistical normalization logic
            print(">>> 🧠 Triggering decentralized normalization logic...")
            for collector in collectors:
                collector.recalculate_platform_stats()

            # 3.5 Top-K Retention Gatekeeper: Keep only the top 100 most intense trends
            print(">>> ✂️ Pruning low-quality posts from database...")
            db_manager.prune_database()     
    
       # 4. Data Health Report
        db_manager.get_db_stats()

        # 5. Final Output - Reporting Results in a formatted table
        print(f">>> ✅ Cycle #{cycle_num} Complete. Total fetched items: {len(all_posts)}")

        print("\n" + "-" * 100)
        print(f"    🏆 GLOBAL TREND RANKING (Cycle #{cycle_num})")
        print("-" * 100)
        print(f"{'Rank':<5} | {'Platform':<12} | {'Score':<6} | {'Title'}")
        print("-" * 100)

        # Fetch top 15 trends from the database
        top_posts = db_manager.get_all_posts()[:15]

        for i, post in enumerate(top_posts, 1):
            title = post.get('title', 'No Title').replace('\n', ' ')

            # Use textwrap for smart shortening without breaking words
            display_title = textwrap.shorten(title, width=100, placeholder="...")

            print(f"#{i:<4} | {post['source_platform']:<12} | {post['trend_score']:>6.1f} | {display_title}")
            print("-" * 100)

    except Exception as e:
        print(f"❌ Critical Error during cycle execution: {e}")


async def start_scheduler():
    """
    Main scheduler loop that keeps the engine running in intervals.
    """
    print("\n" + "#" * 60)
    print(f"      TrendAnalyzer v5.0 - Semantic AI Edition")
    print(f"      🔄 Refresh Interval: {REFRESH_INTERVAL_MINUTES} Minutes")
    print("#" * 60)

    cycle_counter = 1

    while True:
        try:
            current_time = datetime.now().strftime("%H:%M:%S")
            await run_cycle(cycle_counter, current_time)

            print(
                f"\n[Scheduler] 💤 Cycle #{cycle_counter} finished. Sleeping for {REFRESH_INTERVAL_MINUTES} minutes...")
            cycle_counter += 1
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\n🛑 Stopping Scheduler manually...")
            break
        except Exception as e:
            print(f"Unexpected Scheduler Error: {e}")
            print("Retrying in 60 seconds...")
            await asyncio.sleep(60)


if __name__ == "__main__":
    try:
        asyncio.run(start_scheduler())
    except KeyboardInterrupt:
        print("\n👋 System Shutdown.")