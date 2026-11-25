"""送复评流程命令行入口。

用法：
    python3 run_review.py data/corpus.json
    python3 run_review.py data/session_two.json --redesignate "发酵" ferment_defect v2

仅使用标准库；读取外部语料时以路径参数传入，不在语料目录内运行解释器。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 隔离模式（python3 -I）下脚本目录不会自动进 sys.path，显式加入项目根。
# 语料仍只通过命令行路径参数读取，不把语料目录加入模块搜索路径。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_flow.pipeline import ReviewPipeline, load_corpus, render_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="杯测送复评流水线")
    parser.add_argument("corpus", help="语料 JSON 路径")
    parser.add_argument(
        "--redesignate", nargs=3, metavar=("词面", "目标义项编号", "新版本号"),
        help="重新指定词义，例如：--redesignate 发酵 ferment_defect v2",
    )
    args = parser.parse_args(argv)

    corpus = load_corpus(args.corpus)
    pipeline = ReviewPipeline()
    result = pipeline.run(corpus, redesignation=tuple(args.redesignate) if args.redesignate else None)
    print(render_summary(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
