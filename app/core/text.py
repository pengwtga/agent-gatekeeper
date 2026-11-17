"""文本相似度的最小公共件：字符 bigram 集合

检索（app.agents.rag_assistant）与知识库体检（app.kb.validate）用的是同一个口径——
如果两处各写一份，体检说「能召回」而线上检不到，就是最坏的那种「测试通过但功能坏了」。
"""


def bigrams(text: str) -> frozenset[str]:
    """去空白后的字符二元组集合。

    去空白是刻意的：KB 文本里「 3 天」和「3天」应视为同一表达，
    否则空格会制造大量噪声 bigram（" 3"、"3 "）。
    """
    t = "".join(text.split())
    return frozenset(t[i:i + 2] for i in range(len(t) - 1))
