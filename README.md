# Trivia.NET-Async-Server

[![Python 3.10+](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## 📖 Project Overview
**Trivia.NET** is a high-concurrency, asynchronous game server engine built with Python `asyncio`. It implements a custom networked quiz protocol based on **NDJSON (Newline Delimited JSON)** to ensure real-time state synchronization across multiple distributed clients.

The project demonstrates core backend engineering principles: handling non-blocking I/O, managing persistent TCP connections, and ensuring atomic state updates in a single-threaded event loop.

## 🛠️ Technical Highlights
- **Asynchronous Event Loop**: Leverages `asyncio` and non-blocking sockets to handle multiple concurrent client connections without thread-context switching overhead.
- **Custom NDJSON Protocol**: Implemented a robust message framing mechanism using newline delimiters to solve the TCP "sticky packet" and "fragmentation" problems.
- **Real-time State Sync**: Engineered a centralized game manager to synchronize scores, timers, and question distribution across all connected clients with millisecond latency.
- **Robust Exception Handling**: Built-in mechanisms for handling unexpected client disconnections, heartbeat timeouts, and malformed packet recovery.

## 🚀 Quick Start
```bash
# Clone the repository
git clone [https://github.com/Bomai-101/Trivia.NET-Async-Server.git](https://github.com/Bomai-101/Trivia.NET-Async-Server.git)
cd Trivia.NET-Async-Server

# Start the server
python3 server.py --port 8080

.
├── server.py           # Core Event Loop & Socket Handling
├── game_logic.py       # Game State Machine & Scoring
├── protocol.py         # NDJSON Parser & Message Framing
└── tests/              # Concurrency & Stress Tests