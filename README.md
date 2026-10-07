# 乌镇峰会议题协同后端

面向峰会筹备办公室的议题协同后端：多家机构提交议题提案后，系统完成归并、
冲突检查与授权审批，形成不可随意改写的议程版本。运行时不依赖任何外部服务
（纯标准库），状态由事件日志溯源重建。

## 能力总览

| 需求 | 实现 |
| --- | --- |
| 议题提案（责任人/截止时间/保密级别/前置关系） | `submit` 命令 / `POST /proposals` |
| 同一议题被不同工作组重复承诺 | 提交时自动挂靠同一议题并记录 `duplicate_commit` 冲突；协调员可显式 `merge` |
| 重复提交与撤回幂等 | 幂等键 + 同机构同议题自然幂等；重复撤回无副作用 |
| 截止时间修订链 | 每次修订生成链式摘要（prev_digest 衔接），可独立校验 |
| 材料摘要按角色可见 | RBAC：角色 clearance × 保密级别 × 机构归属，越权自动脱敏 |
| 不可改写的议程版本 | `agenda_published` 事件快照 + 版本间摘要链，篡改启动即发现 |
| 重放任一议程版本 | `replay` 命令 / `GET /agendas/{v}/replay`，附链式校验 |
| 解释合并/延期/拒绝/移交 | `explain` 命令 / `GET /topics/{id}/explain`，时间线 + 结论 |
| 重启后继续处理未完成审批 | 事件溯源：打开数据目录即重放恢复全部状态 |

## 运行测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 编译检查

```bash
python3 -m compileall -q src tests run_cli.py
```

## 快速体验

```bash
python3 run_cli.py demo          # 端到端演示：提交→归并→审批→发布→重放→解释
python3 run_cli.py serve --port 8080   # 启动 HTTP 服务
```

## 命令行（CLI）

全局参数：`--data-dir`（数据目录，默认 `.summit_data`）、`--actor`、`--role`
（submitter/coordinator/approver/auditor）、`--org`。

```bash
# 提交（幂等键必填；--depends-on 可多次指定前置议题）
python3 run_cli.py --role submitter --org 机构A submit \
    --idem k-1 --title 数字丝路 --summary 摘要 --org 机构A --workgroup 甲组 \
    --owner 张三 --deadline 2026-11-15 --secrecy internal

python3 run_cli.py withdraw P-1 --idem k-2 --reason 计划调整      # 撤回（幂等）
python3 run_cli.py revise-deadline P-1 --to 2026-11-30 --reason 延后 --idem k-3
python3 run_cli.py revisions P-1                                 # 查看修订链
python3 run_cli.py merge --survivor T-1 --merged T-2 --reason 重复承诺 --idem k-4
python3 run_cli.py defer T-1 --until 2027-01-10 --reason 等预算 --idem k-5
python3 run_cli.py transfer T-1 --to-workgroup 乙组 --reason 职责划转 --idem k-6
python3 run_cli.py request-approval T-1 --idem k-7               # 前置未通过会被拒
python3 run_cli.py --role approver decide AP-1 --decision approved --idem k-8
python3 run_cli.py publish --idem k-9                            # 发布议程版本
python3 run_cli.py replay 1                                      # 重放议程 v1
python3 run_cli.py explain T-1                                   # 解释处置过程
python3 run_cli.py pending                                       # 未完成审批
python3 run_cli.py conflicts                                     # 冲突检查
python3 run_cli.py verify                                        # 校验全部链
```

## HTTP 接口

请求头：`X-Actor-Id`、`X-Actor-Role`、`X-Actor-Org`（中文值需 percent-encoding）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/proposals` | 提交提案（body 含 idem/title/summary/org/workgroup/owner/deadline/secrecy/depends_on） |
| POST | `/proposals/{id}/withdraw` | 撤回提案 |
| POST | `/proposals/{id}/deadline` | 修订截止时间 |
| GET | `/proposals` | 提案列表（按角色脱敏） |
| GET | `/proposals/{id}/revisions` | 截止时间修订链 |
| GET | `/topics` / `/topics/{id}` | 议题列表/详情 |
| GET | `/topics/{id}/explain` | 解释议题处置过程 |
| POST | `/topics/merge` | 归并议题 |
| POST | `/topics/{id}/defer` / `/transfer` / `/request-approval` | 延期/移交/发起审批 |
| POST | `/approvals/{id}/decide` | 审批决定 |
| GET | `/approvals?status=pending` | 未完成审批 |
| POST | `/agendas/publish` | 发布议程版本 |
| GET | `/agendas` / `/agendas/{v}/replay` | 版本列表/重放 |
| GET | `/conflicts` | 冲突检查 |
| GET | `/verify` | 校验事件链/议程链/修订链 |
| GET | `/health` | 健康检查（无需身份） |

## 角色与保密级别

- 角色：`submitter`（提交人，仅本机构）、`coordinator`（会务协调）、
  `approver`（审批人）、`auditor`（只读审计）。
- 保密级别：`public < internal < confidential < secret`。
- 摘要可见性 = 角色 clearance 不低于材料级别；提交人另受机构归属约束。

## 架构

```
src/summit_agenda/
  domain.py   枚举/错误/规范化/不可变值对象
  events.py   事件类型与规范化序列化、链式摘要
  store.py    只增不改的事件日志（JSONL + 启动校验）
  rbac.py     命令权限与摘要脱敏
  service.py  命令处理 + 事件溯源投影 + 查询/解释（应用核心）
  api.py      标准库 HTTP 接口
  cli.py      命令行
```

所有状态变更先追加事件（携带幂等键与命令结果），再应用到内存投影；
服务重启时重放日志即可恢复，包括未完成的审批。事件、议程版本、修订链
均采用链式摘要，任何篡改都会在启动校验或 `verify` 时暴露。
