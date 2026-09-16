"""出包时被 `git archive` 填充的构建标记（见仓库根的 .gitattributes export-subst）。

clone 出来的工作树里这两个值保持字面量 `$Format:…$`，`core/version.py` 会退回
去问 git。所以同一份代码在「tarball」和「git 克隆」两种形态下都能报出版本。
"""

COMMIT = "$Format:%H$"
DATE = "$Format:%cI$"
