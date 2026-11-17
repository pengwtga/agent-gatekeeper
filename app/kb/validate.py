"""知识库入库校验（T5-05）：把「人肉约定」变成可执行的断言

问题：KB 条目此前只有 `json.loads` 一层。类名写错（`ea_timeout` vs `ea_sync_timeout`）
照样加载、照样能召回，答案里就出现一个分类字典里不存在的类名；同簇条目动作写岔也没人拦。
正确性靠作者小心，不靠代码——150 条时是「现在对」，不是「改不错」。

两种校验，成本差两个数量级，所以分层：

| 层 | 复杂度 | 跑在哪 | 查什么 |
|---|---|---|---|
| 结构校验 | O(n) | **启动期** + CLI + CI | 解析、必填、类型、空值、类名枚举、ID 唯一、分组一致 |
| 语义体检 | O(n²) | 仅 CLI/CI | 簇内自召回：这条能被同簇条目召回吗 |

为什么语义体检不进启动期：它要对每一条做一次全库比对，10k 条时是 10⁸ 次集合运算，
把启动拖成分钟级。启动期只做「保证服务不带病上线」的 O(n) 层；O(n²) 那层交给提交前。

判定与修复分开：本模块只报告，不改数据。自动修复类名（拿最接近的枚举值顶上）看着聪明，
但会把「作者理解错了」这件事静默改写掉——知识库的错误应该被人看见。
"""

import argparse
import difflib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from app.core.text import bigrams

logger = logging.getLogger(__name__)

# ── 错误码：CI/脚本按码过滤比匹配文案稳 ──
E_PARSE = "E_PARSE"                    # 行不是合法 JSON
E_NOT_OBJECT = "E_NOT_OBJECT"          # 解析出来不是对象（数组/字符串）
E_MISSING_FIELD = "E_MISSING_FIELD"    # 必填字段缺失
E_BAD_TYPE = "E_BAD_TYPE"              # 字段不是字符串
E_EMPTY_FIELD = "E_EMPTY_FIELD"        # 字段是空串/纯空白
E_CLASS_UNKNOWN = "E_CLASS_UNKNOWN"    # 类名不在领域枚举内 ← 本项目最想拦的一类
E_DUP_ID = "E_DUP_ID"                  # 主键重复
E_INCONSISTENT = "E_INCONSISTENT"      # 同组条目某字段不一致
E_NOT_RECALLABLE = "E_NOT_RECALLABLE"  # 簇内自召回失败（仅 --recall）
W_NO_CLASS_ENUM = "W_NO_CLASS_ENUM"    # 没给枚举（类名无从校验，等于少了一道关）
W_SHORT_FIELD = "W_SHORT_FIELD"        # 字段过短（空壳条目的下限，不判语义）
W_SINGLETON_CLASS = "W_SINGLETON_CLASS"  # 某类只有 1 条，自召回无从验证

# 长度下限只拦「空壳」：取值低于当前三库实测最小值（title 4 / symptom 11 / resolution 17）
_MIN_LEN = {"title": 4, "symptom": 10, "resolution": 15}


@dataclass
class Issue:
    level: str            # error | warning
    path: Path
    line: int             # 1-based；0 表示文件级
    code: str
    field: Optional[str]
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "file": str(self.path), "line": self.line,
                "code": self.code, "field": self.field, "message": self.message}

    def __str__(self) -> str:
        loc = f"{self.path.name}:{self.line}" if self.line else self.path.name
        return f"{loc}  {self.code}  {self.field or '-'}  {self.message}"


class KBValidationError(ValueError):
    """知识库不合格。启动期抛它 = 拒绝带病上线；CLI 抛它 = 退出码 1。"""

    def __init__(self, path: Path, issues: Sequence[Issue]):
        self.path = path
        self.issues = list(issues)
        head = "；".join(str(i) for i in self.issues[:5])
        more = f"…（共 {len(self.issues)} 条）" if len(self.issues) > 5 else ""
        super().__init__(f"知识库校验失败 {path}：{head}{more}")


# ── 解析 ──
def _parse(path: Path) -> tuple[list[tuple[int, dict[str, Any]]], list[Issue]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    issues: list[Issue] = []
    if not path.exists():
        issues.append(Issue("error", path, 0, E_PARSE, None, "文件不存在"))
        return rows, issues
    with path.open(encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            if not raw.strip():
                continue  # 空行放过：JSONL 里常见，不算数据
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as e:
                issues.append(Issue("error", path, lineno, E_PARSE, None, f"JSON 解析失败：{e.msg}"))
                continue
            if not isinstance(obj, dict):
                issues.append(Issue("error", path, lineno, E_NOT_OBJECT, None,
                                    f"行内容应为对象，实际是 {type(obj).__name__}"))
                continue
            rows.append((lineno, obj))
    return rows, issues


# ── 结构校验（O(n)，启动期也跑） ──
def _check_structure(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    path: Path,
    id_field: str,
    class_field: str,
    classes: Optional[dict[str, str]],
    required_fields: Sequence[str],
    consistency: Sequence[Any],
    min_len: dict[str, int] = _MIN_LEN,
) -> list[Issue]:
    issues: list[Issue] = []
    need = (id_field, class_field, *required_fields)
    first_seen: dict[str, int] = {}

    for lineno, row in rows:
        for f in need:
            if f not in row:
                issues.append(Issue("error", path, lineno, E_MISSING_FIELD, f, "必填字段缺失"))
                continue
            v = row[f]
            if not isinstance(v, str):
                issues.append(Issue("error", path, lineno, E_BAD_TYPE, f,
                                    f"应为字符串，实际 {type(v).__name__}"))
            elif not v.strip():
                issues.append(Issue("error", path, lineno, E_EMPTY_FIELD, f, "空值"))
            elif f in min_len and len(v.strip()) < min_len[f]:
                issues.append(Issue("warning", path, lineno, W_SHORT_FIELD, f,
                                    f"仅 {len(v.strip())} 字（下限 {min_len[f]}）：确认不是占位内容"))

        cls = row.get(class_field)
        if isinstance(cls, str) and cls and classes is not None and cls not in classes:
            hint = difflib.get_close_matches(cls, list(classes), n=1, cutoff=0.6)
            tail = f"；最接近 '{hint[0]}'" if hint else ""
            issues.append(Issue("error", path, lineno, E_CLASS_UNKNOWN, class_field,
                                f"'{cls}' 不在 {len(classes)} 类枚举内{tail}"))

        key = row.get(id_field)
        if isinstance(key, str) and key.strip():
            if key in first_seen:
                issues.append(Issue("error", path, lineno, E_DUP_ID, id_field,
                                    f"'{key}' 与第 {first_seen[key]} 行重复"))
            else:
                first_seen[key] = lineno

    issues.extend(_check_consistency(rows, path, consistency))
    return issues


def _check_consistency(rows, path, constraints) -> list[Issue]:
    """分组一致性：同 group_by 值的条目，same 里的字段必须相同。

    ssrm 按 root_cause_class 分组，要求 standard_action 一致（一类一动作）；
    csa/credit 按 cluster_key 分组，要求 category 一致（簇不跨类）。
    两种形态用同一条规则表达，领域声明参数即可，校验器不认识具体字段名。
    """
    issues: list[Issue] = []
    for rule in constraints:
        group_by = getattr(rule, "group_by", None)
        same = getattr(rule, "same", ())
        if not group_by:
            continue
        seen: dict[tuple[str, str], tuple[str, int]] = {}
        for lineno, row in rows:
            gv = row.get(group_by)
            if not isinstance(gv, str) or not gv:
                continue
            for f in same:
                v = row.get(f)
                if not isinstance(v, str):
                    continue
                got = seen.setdefault((gv, f), (v, lineno))
                if v != got[0]:
                    issues.append(Issue(
                        "error", path, lineno, E_INCONSISTENT, f,
                        f"{group_by}='{gv}' 的该字段应为「{got[0]}」（见第 {got[1]} 行），"
                        f"实际「{v}」；同组条目必须共享该字段"))
    return issues


# ── 语义体检（O(n²)，仅 CLI/CI） ──
def _check_recall(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    path: Path,
    class_field: str,
    top_k: int = 3,
) -> tuple[list[Issue], int, int]:
    """簇内自召回：拿一条自己的 title+symptom 当 query，同簇其它条目能否进 Top-K。

    这条检的是「写得够不够像人话」——脚本判不了语义，但能判「这条和同簇的其它条目
    用的是不是同一套词汇」。措辞完全另起炉灶的条目，线上同样召不回同类案例。
    用 bigram 覆盖率、不调 embedding/LLM：确定性、零成本、CI 里可跑。

    返回 (issues, 参检条数, 通过条数)。
    """
    grams = [bigrams(f"{r.get('title', '')}{r.get('symptom', '')}") for _, r in rows]
    by_class: dict[str, int] = {}
    for _, r in rows:
        c = r.get(class_field)
        if isinstance(c, str):
            by_class[c] = by_class.get(c, 0) + 1

    issues: list[Issue] = []
    checked = passed = 0
    for i, (lineno, row) in enumerate(rows):
        cls = row.get(class_field)
        if by_class.get(cls, 0) < 2:
            continue  # 单条成簇，本次跳过（结构层已给 W_SINGLETON_CLASS）
        checked += 1
        q = grams[i]
        scored = sorted(
            ((len(q & g) / max(len(q), 1), j) for j, g in enumerate(grams) if j != i),
            key=lambda t: (-t[0], t[1]),
        )[:top_k]
        if any(rows[j][1].get(class_field) == cls for _, j in scored):
            passed += 1
        else:
            top = scored[0] if scored else (0.0, i)
            issues.append(Issue(
                "error", path, lineno, E_NOT_RECALLABLE, class_field,
                f"Top{top_k} 内无同簇条目（最高命中 {top[0]:.2f} 是 "
                f"{rows[top[1]][1].get(class_field)}）：描述用词与同簇条目不重合，线上同样召不回"))
    return issues, checked, passed


# ── 对外接口 ──
def _validate_numbered(
    rows: Sequence[tuple[int, dict[str, Any]]],
    *,
    path: Path,
    id_field: str,
    class_field: str,
    classes: Optional[dict[str, str]],
    required_fields: Sequence[str],
    consistency: Sequence[Any],
    recall: bool,
) -> list[Issue]:
    """主流程：入参是「行号 + 条目」，所以文件里的行号能原样带到报错里"""
    issues = _check_structure(
        rows, path=path, id_field=id_field, class_field=class_field,
        classes=classes, required_fields=required_fields, consistency=consistency,
    )
    if classes is None:
        issues.append(Issue("warning", path, 0, W_NO_CLASS_ENUM, class_field,
                            "未提供类名枚举，类名拼写无从校验"))
    for cls, n in _class_counts(rows, class_field).items():
        if n == 1:
            issues.append(Issue("warning", path, 0, W_SINGLETON_CLASS, class_field,
                                f"'{cls}' 只有 1 条，无同簇参照"))
    if recall:
        more, _, _ = _check_recall(rows, path=path, class_field=class_field)
        issues += more
    return issues


def validate_rows(
    rows: Sequence[dict[str, Any]],
    *,
    id_field: str,
    class_field: str,
    classes: Optional[dict[str, str]] = None,
    required_fields: Sequence[str] = (),
    consistency: Sequence[Any] = (),
    path: Optional[Path] = None,
    recall: bool = False,
) -> list[Issue]:
    """内存入口：给单测和「提交前只想校验手上这几条」的脚本用，不碰磁盘。

    行号按 1..n 计（等价于「每条一行」的 JSONL），和文件入口口径一致。
    """
    numbered = [(i + 1, r) for i, r in enumerate(rows)]
    return _validate_numbered(
        numbered, path=Path(path) if path else Path("<memory>"), id_field=id_field,
        class_field=class_field, classes=classes, required_fields=required_fields,
        consistency=consistency, recall=recall,
    )


def validate_file(
    path: Path,
    *,
    id_field: str,
    class_field: str,
    classes: Optional[dict[str, str]] = None,
    required_fields: Sequence[str] = (),
    consistency: Sequence[Any] = (),
    recall: bool = False,
) -> list[Issue]:
    path = Path(path)
    numbered, parse_issues = _parse(path)
    return parse_issues + _validate_numbered(
        numbered, path=path, id_field=id_field, class_field=class_field,
        classes=classes, required_fields=required_fields,
        consistency=consistency, recall=recall,
    )


def _class_counts(rows, class_field) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _, r in rows:
        c = r.get(class_field)
        if isinstance(c, str):
            counts[c] = counts.get(c, 0) + 1
    return counts


def _spec_kwargs(spec) -> dict[str, Any]:
    """从领域规格里取校验参数。用 getattr 带默认值：校验器属于机制层，
    不该因为领域还没声明某个约束就崩（老 pack 不写新字段也要能跑）。
    """
    return dict(
        id_field=getattr(spec, "id_field", "id"),
        class_field=getattr(spec, "class_field", "category"),
        classes=getattr(spec, "classes", None),
        required_fields=getattr(spec, "required_fields", ()) or (),
        consistency=getattr(spec, "consistency", ()) or (),
    )


def validate_spec(spec, *, recall: bool = False) -> list[Issue]:
    """按领域规格校验其知识库（spec 为 duck-typed：AssistantSpec 即可）。"""
    return validate_file(Path(spec.kb_file), recall=recall, **_spec_kwargs(spec))


def load_kb(spec, *, recall: bool = False) -> list[dict[str, Any]]:
    """读取并校验知识库；有 error 直接抛 KBValidationError（启动期 = 拒绝带病上线）。

    warning 不拦：长度、单条成簇这些是「提醒作者看一眼」，不是「数据错了」。
    """
    path = Path(spec.kb_file)
    numbered, parse_issues = _parse(path)
    issues = parse_issues + _validate_numbered(
        numbered, path=path, recall=recall, **_spec_kwargs(spec))
    errors = [i for i in issues if i.level == "error"]
    if errors:
        logger.error("知识库校验失败，拒绝加载",
                     extra={"event": "kb_invalid", "stage": "kb", "file": str(path),
                            "errors": len(errors), "warnings": len(issues) - len(errors),
                            "first": str(errors[0]), "outcome": "fail"})
        raise KBValidationError(path, errors)
    warnings = [i for i in issues if i.level == "warning"]
    if warnings:
        logger.info("知识库校验通过（有提醒）",
                    extra={"event": "kb_warn", "stage": "kb", "file": str(path),
                           "warnings": len(warnings), "first": str(warnings[0]), "outcome": "ok"})
    return [r for _, r in numbered]


# ── CLI ──
def _registered_specs() -> dict[str, Any]:
    """只导入领域包，不导入 app.main —— main 会建 LLM 客户端、开 SQLite，CLI 不需要。"""
    from app.domains.credit import pack as credit_pack
    from app.domains.csa import pack as csa_pack
    from app.domains.ssrm import pack as ssrm_pack

    specs = {}
    for p in (ssrm_pack, csa_pack, credit_pack):
        if p.assistant is not None:
            specs[p.name] = p.assistant
    return specs


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m app.kb.validate",
        description="校验知识库 JSONL（结构 O(n)；--recall 追加簇内自召回 O(n²)）",
    )
    ap.add_argument("path", nargs="?", help="KB 文件路径；省略则校验全部已注册领域")
    ap.add_argument("--domain", help="领域名（ssrm/csa/credit），提供类名枚举与分组约束")
    ap.add_argument("--recall", action="store_true", help="追加簇内自召回体检（O(n²)，CI 用）")
    ap.add_argument("--json", action="store_true", dest="as_json", help="输出 JSON（给 CI 解析）")
    args = ap.parse_args(list(argv) if argv is not None else None)

    specs = _registered_specs()
    if args.path:
        if not args.domain:
            # 光有文件不知道 id/类名字段叫什么、枚举是什么，报出来的会是满屏假错
            print("必须同时指定 --domain（需要领域声明 id 字段、类名字段与枚举）；"
                  f"可选：{', '.join(specs)}", file=sys.stderr)
            return 2
        if args.domain not in specs:
            print(f"未知领域 '{args.domain}'，可选：{', '.join(specs)}", file=sys.stderr)
            return 2
        targets = [(args.domain, Path(args.path), specs[args.domain])]
    else:
        targets = [(name, Path(s.kb_file), s) for name, s in specs.items()]

    all_issues: list[Issue] = []
    summary: list[dict[str, Any]] = []
    for name, path, spec in targets:
        issues = validate_spec(spec, recall=args.recall)
        rows, _ = _parse(path)
        errors = [i for i in issues if i.level == "error"]
        warns = [i for i in issues if i.level == "warning"]
        all_issues += issues
        summary.append({"domain": name, "file": str(path), "rows": len(rows),
                        "errors": len(errors), "warnings": len(warns)})
        if args.as_json:
            continue
        mark = "PASS" if not errors else "FAIL"
        print(f"[{mark}] {name:<10} {path}  {len(rows)} 条 · {len(errors)} error · {len(warns)} warning")
        for i in issues:
            print(f"       {i}")

    if args.as_json:
        print(json.dumps({"summary": summary,
                          "issues": [i.as_dict() for i in all_issues]},
                         ensure_ascii=False, indent=2))
    else:
        total = sum(s["errors"] for s in summary)
        print(f"\n合计 {len(summary)} 个文件 / {sum(s['rows'] for s in summary)} 条 / {total} 个错误")
    return 0 if not any(i.level == "error" for i in all_issues) else 1


if __name__ == "__main__":
    sys.exit(main())
