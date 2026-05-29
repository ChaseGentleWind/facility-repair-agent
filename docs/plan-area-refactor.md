# Plan: 用 `area` 取代 `unit`，强化楼层自动推断

## Context

当前 `TicketDraft.unit` 语义是「单元/座/塔」（"1单元"、"A座"、"东塔"），但业务真正常见的是复合区域编号（`2-L28`、`8-2401`、`2-701A`）。这类编号现在被错误地塞进 `room`，由 `_infer_floor_from_room` 用正则推楼层；而楼栋（`T2`/`T8`）信息**完全被丢弃**，必须靠用户额外补充。

本次改动重新对齐字段语义：

- `unit` 字段改名为 `area`，专表「区域编号」
- `area` 与 `room` 互斥：复合编号（含 `-`）归 `area`，纯房号归 `room`
- 从 `area` 同时推断 `building`（`T<n>`）与 `floor`，从 `room` 仅推断 `floor`
- 三个必填仍为 `description / estate / building / floor / visit_time`，`area` 与 `room` 都可选
- 大堂/大厅/走廊等公共区域：`area` 留空，由 `description` 自然带出，不强制追问

预期结果：用户给"2-L28空调坏了"时，系统自动得到 `building=T2 / floor=L28楼 / area=2-L28 / room=null`，无需追问；给"4505灯不亮"时自动得到 `floor=45楼 / room=4505`。

## 推断规则速查

### area 模式（含 `-`）

| 输入 | building | floor | area |
|---|---|---|---|
| `2-L28` | T2 | L28 楼 | 2-L28 |
| `2-L29会议室` | T2 | L29 楼 | 2-L29会议室 |
| `2-B05` | T2 | B5（地下） | 2-B05 |
| `8-2401` | T8 | 24 楼 | 8-2401 |
| `2-701A` | T2 | 7 楼 | 2-701A |
| `3-B05停车场` | T3 | B5（地下） | 3-B05停车场 |

仅在对应 `draft` 字段为空时填入，不覆盖用户已提供的值。

### room 模式（无 `-`）

| 输入 | floor | room |
|---|---|---|
| `4505` | 45 楼 | 4505 |
| `2207` | 22 楼 | 2207 |
| `1205` | 12 楼 | 1205 |
| `302` | 3 楼 | 302 |
| `7S1` | 7 楼 | 7S1 |
| `12B5` | 12 楼 | 12B5 |
| `1309房间` | 13 楼 | 1309房间 |

### 公共区域

`大堂`、`大厅`、`走廊`、`电梯间`、`卫生间` 等若用户未给具体编号 → `area=null, room=null`，信息保留在 `description` 里（如 `"大堂漏水"`）。`building/floor` 仍必填，缺则追问。

## 改动清单

### 1. `backend/app/agent/state.py`

- `TicketDraft.unit: str | None` → `area: str | None`
- `to_dict()` 中 `"unit"` → `"area"`
- `missing_required()` 不变（area/room 仍非必填）

### 2. `backend/app/agent/prompts.py`

`EXTRACTION_SYSTEM` 提取规则段：

- 删除原 `unit` 段
- 新增 `area` 段：
  ```
  - area：区域编号（与 room 互斥，含连字符的复合编号优先归此字段）。常见格式：
    · 楼栋号-楼层号-房间号：2-L28、8-2401、2-701A、3-B05
    · 带后缀：2-L29会议室、3-B05停车场
    · ⚠️ 含 `-` 的编号一律归 area，不要拆分到 room
  ```
- 修改原 `room` 段：明确"不含连字符"；移除 `2-L29` / `3-B05` 这两个旧示例

JSON Schema 字段 `unit` → `area`。

提取示例同步更新：
- 删除 `"前海嘉里中心T25栋3楼1单元"` 中 `unit="1单元"` 这一项（改成 area=null）；如需展示 area 用法，加一条 `"T25栋2-L28空调坏了"` 的示例
- 新增：`"2-L28空调坏了" → area="2-L28", building=null, floor=null, room=null, description="空调坏了"`（building/floor 留给规则推断，不让 LLM 抢推断）
- 新增：`"8-2401漏水" → area="8-2401", description="漏水"`
- 新增：`"4505灯不亮" → room="4505", description="灯不亮"`
- 修改：`"2-L29会议室空调噪音大"` 输出从 `room` 改为 `area="2-L29会议室"`
- 修改：`"3-B05停车场漏水"` 输出从 `room` 改为 `area="3-B05停车场"`
- 修改：`"大堂漏水" → description="大堂漏水", area=null, room=null`

`editing_extract_prompt` 增加一条："如果用户只提到了 area（如 '2-L28'）或 room，但没有明确提到楼栋/楼层，building/floor 字段必须返回 null（保留原值），由系统自动推断。"

### 3. `backend/app/agent/core.py`

**重命名/扩展推断函数**：把 `_infer_floor_from_room` 改造为 `_infer_location_from_area_or_room(extraction, draft)`：

- 先看 `extraction["area"]`：若非空且 draft 中 building/floor 为空，按上表正则填入 `extraction["building"]` / `extraction["floor"]`
  - 正则建议：
    - `^(\d+)-([LB])(\d+)` → building=`T{1}`, floor=`{2}{3}`（`L`→ 层，`B`→ 地下）
    - `^(\d+)-(\d{4})` → building=`T{1}`, floor=`{2前两位}楼`
    - `^(\d+)-(\d{1,3})[A-Z]?\d*` → building=`T{1}`, floor=`{2第1位}楼`
- 再看 `extraction["room"]`：保留现有 3 位/4 位/数字字母混编规则
- area 与 room 互斥，若两者都被填，记 warning 并保留 area，清空 room（防止 LLM 误判）

**`_apply_extraction`**：
- `unit` → `area`：`if extraction.get("area"): draft.area = extraction["area"]`

**`_handle_preview_ready`**：
- 新增 `area_changed = new_area is not None and new_area != session.draft.area`
- room 修改检测保留；如 area 变了则同样可能改 building/floor，应用前调用新 `_infer_location_from_area_or_room`
- 修改场景下若 area 与 room 互相切换，需清掉对侧字段

调用点替换：原 `_infer_floor_from_room(extraction, session.draft)` 三处全部改为新的 `_infer_location_from_area_or_room`。

### 4. `backend/app/agent/ticket_builder.py`

`location` 字典：
```python
"location": {
    "estate": draft.estate,
    "building": draft.building,
    "floor": draft.floor,
    "area": draft.area,
    "room": draft.room,
},
```
（删除 `unit`）

### 5. `frontend/index.html`

按"共享一个输入框"方案：

- 第 4 列输入框 `id="f-room"` 标签从「房间」改为「区域/房间」（共享）
- `renderTicket`：
  ```js
  document.getElementById('f-room').value = loc.area || loc.room || ''
  ```
- `collectTicketFromForm`：根据输入内容形态判断回写到 area 还是 room
  ```js
  const locInput = document.getElementById('f-room').value.trim()
  const isArea = locInput.includes('-')
  // location 字段
  area: isArea ? (locInput || null) : null,
  room: isArea ? null : (locInput || null),
  ```
  并把 `unit: baseLoc.unit || null` 这一行删除
- `lockTicket` / `unlockTicket` 中针对 `f-room` 的禁用语句保留即可（共享输入框），无需额外加 area 控件

### 6. `backend/app/api/v1/ticket.py`

不需改动（`session.ticket["location"]` 整体覆盖，已包含新字段）。

### 7. `frontend/src/types.ts`

未引用 location 子字段，无改动。

## 关键文件路径

- `backend/app/agent/state.py`
- `backend/app/agent/prompts.py`
- `backend/app/agent/core.py`
- `backend/app/agent/ticket_builder.py`
- `frontend/index.html`

## 验证

### 单元级（手工跑提取）
列出以下消息逐条丢给 `_handle_collecting`，检查最终 `draft.to_dict()`：

| 输入 | 期望 |
|---|---|
| "2-L28空调坏了" | building=T2, floor=L28（或 L28楼）, area=2-L28, room=null, description=空调坏了 |
| "8-2401漏水" | building=T8, floor=24楼, area=8-2401, room=null, description=漏水 |
| "2-701A灯不亮" | building=T2, floor=7楼, area=2-701A, room=null, description=灯不亮 |
| "4505灯不亮" | floor=45楼, room=4505, area=null |
| "2207漏水" | floor=22楼, room=2207, area=null |
| "T25栋3楼302" | building=T25栋, floor=3楼, room=302, area=null |
| "大堂漏水" | description=大堂漏水, area=null, room=null（仍会追问 building/floor） |
| 在 PREVIEW_READY 改"改成 8-2401" | area=8-2401, building=T8, floor=24楼，原 room 清空 |

### 端到端
1. 后端：`cd backend && uvicorn app.main:app --reload`
2. 前端：`cd frontend && npm run dev`
3. 走两条会话：
   - "前海嘉里中心 2-L28 空调不制冷" → 不应追问楼栋/楼层；确认后预览卡片「楼栋」=T2、「楼层」=L28、「区域/房间」=2-L28
   - "前海嘉里中心 4505 灯不亮" → 不应追问楼层；预览卡片「楼层」=45楼、「区域/房间」=4505
4. 在 PREVIEW_READY 状态聊天框输入"区域改成 8-2401"，预览卡片应自动同步 building=T8 / floor=24楼
5. 在工单卡片里手动把「区域/房间」改成 `2207` 后提交，后端日志的 `session.ticket["location"]` 应是 `{room: "2207", area: null, ...}`

### 回归
- `B1` 单独出现时仍归 floor（与 area 规则不冲突）
- `T25栋3楼302` 三段齐全 → building/floor 不被推断逻辑覆盖
- 大堂/走廊场景不被强制追问 area
