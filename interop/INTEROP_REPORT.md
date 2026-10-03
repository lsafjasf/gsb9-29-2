# 互操作验证报告

## 判定标准

- 内容：普通文件比较 SHA-256；符号链接比较链接目标；条目缺失/多出记为内容维度不一致
- 顺序：读取方条目名序列必须与写入方写出序列完全一致
- 权限：st_mode & 0o7777（符号链接除外，tar 惯例固定 0777，跨工具不可比）
- 时间：整数秒 mtime，允许 ±1s 容差
- 类型：file / dir / symlink 必须一致

## 互操作矩阵

| 用例 | 写入方 | 读取方 | 内容 | 顺序 | 权限 | 时间 | 类型 | 结论 |
|---|---|---|---|---|---|---|---|---|
| full | mytar | gnu-tar | OK | OK | OK | OK | OK | PASS |
| full | mytar | py-tarfile | OK | OK | OK | OK | OK | PASS |
| full | gnu-tar | mytar | OK | OK | OK | OK | OK | PASS |
| full | py-tarfile | mytar | OK | OK | OK | OK | OK | PASS |
| empty | mytar | gnu-tar | OK | OK | OK | OK | OK | PASS |
| empty | mytar | py-tarfile | OK | OK | OK | OK | OK | PASS |
| empty | gnu-tar | mytar | OK | OK | OK | OK | OK | PASS |
| empty | py-tarfile | mytar | OK | OK | OK | OK | OK | PASS |

## 差异证据

（本次运行无不一致）

## 差异证据格式样例（自测注入构造，非本次运行结果）

- entry='<archive>' field=order expected=index 0: ['a.txt'] (共 2 条) actual=index 0: ['b.txt'] (共 2 条)
- entry='a.txt' field=mtime expected=1696000000 actual=1696000005
- entry='b.txt' field=content expected=3e744b9dc39389baf0c5a0660589b8402f3dbb49b89b3e75f2c9355852a3c677 actual=9b38b8f5877f2395b4361c1f68c059078ee9c0c8b0cbb22c97d2906e011e40a3
- entry='b.txt' field=mode expected=0o600 actual=0o644

## 边界用例

- 长文件名：路径全长 >100 字节（ustar name 字段上限），含多级长目录，触发 GNU longlink / pax path 扩展
- 非 ASCII：中文目录名、中文+拉丁扩展字符文件名、UTF-8 文件内容
- 稀疏文件：4194304 字节逻辑大小，仅首尾有数据，中间为空洞（按内容全零判定）
- 空归档：0 个条目的归档（mytar/tarfile 直接写零块，GNU tar 用 --files-from /dev/null）
- 其他：空文件、符号链接、自定义权限（0640/0600/0750）、固定 mtime

## 运行方式

```sh
python3 interop/interop_check.py     # 运行互操作矩阵并生成本报告
python3 -m unittest discover -s interop -v   # 运行自测
```
