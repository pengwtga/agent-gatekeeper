"""让 pytest 从仓库根导入 app 包（tests/ 目录运行时）"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
