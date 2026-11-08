# `POST /retrieval`: giải thích code

## ELI5

`/chat` đã điền xong **phiếu yêu cầu** (`extraction`). `/retrieval` là người thủ thư cầm phiếu đi
tìm trong sổ địa điểm TP.HCM:

1. **Tìm theo tên**: phiếu ghi "phải ghé Dinh Độc Lập" → tra đúng nơi đó, ghim lên đầu. Tên mơ hồ
   (nhiều nơi giống nhau) → hỏi lại "ý bạn là nơi nào?". Không thấy → báo không thấy, **không bao
   giờ tự đổi sang một nơi "na ná"**.
2. **Tìm theo sở thích**: "thích cà phê" → lấy kệ "Cà phê". "Thích phở" không có kệ riêng → tìm
   chữ "phở" trong tên và mô tả, rồi xếp kết quả vào kệ phù hợp nhất.
3. **Gạch bỏ**: nơi cần tránh, nơi đóng cửa vào lúc người dùng rảnh.
4. **Chấm điểm và xếp hàng**: nơi khớp tốt, mở cửa đúng giờ, gần chỗ đã chọn thì lên trước. Chất
   lượng chỉ là ngưỡng phải đạt, không phải điểm xếp hạng; các nơi phù hợp ngang nhau được xáo theo
   `seed`. Mỗi kệ không để một kiểu quán (hay nơi nổi tiếng) chiếm hết chỗ.
5. **Giải thích**: mỗi gợi ý kèm lý do lấy từ dữ liệu ("Michelin Bib Gourmand", "Mở 06:00–22:00
   thứ Bảy"), không có câu nào do AI viết.

Không có LLM ở đây. Mọi thứ là SQL và Python thuần, nên cùng một phiếu luôn cho cùng một kết quả.

## Hai giai đoạn: lúc import và lúc request

Muốn hiểu `/retrieval` phải biết rằng **nhiều việc đã được làm sẵn từ lúc import dữ liệu**, để lúc
request chỉ còn tra cứu.

```
LÚC IMPORT (chạy tay, offline)                  LÚC REQUEST (mỗi lần gọi API)
──────────────────────────────                  ─────────────────────────────
data/places_1000.parquet                         extraction từ /chat
   │ scripts/import_parquet.py                      │
   │ ingest.prepare_row()                           │ service.retrieve()
   │   - tính name_keys, name_lex, search_lex       │   - tra tên trên name_keys
   │   - sửa category theo poi_overrides.csv        │   - full-text trên name_lex/search_lex
   │   - kiểm tra giờ mở cửa đọc được              │   - lọc theo category
   ▼                                                │   - giờ, điểm, section trong Python
Postgres bảng pois (db/schema.sql)  ◄───────────────┘
```

Hàm biến đổi văn bản (`text.py`) dùng chung cho cả hai phía, nên cách import "viết" và cách
request "đọc" không thể lệch nhau.

## Bản đồ file

| File | Vai trò | I/O? |
|---|---|---|
| [routes/retrieval.py](../../app/api/routes/retrieval.py) | Route: mượn kết nối từ pool, gọi `retrieve`, đổi lỗi DB thành 503 | Pool |
| [schemas/retrieval.py](../../app/schemas/retrieval.py) | Request (giới hạn kích thước list) và response | Không |
| [services/retrieval/service.py](../../app/services/retrieval/service.py) | **Điều phối**: `retrieve()` gọi mọi bước theo thứ tự | Có |
| [services/retrieval/interests.py](../../app/services/retrieval/interests.py) | Phân loại mỗi sở thích: kệ chuẩn, bộ lọc cột, hay tìm chữ | Không |
| [services/retrieval/names.py](../../app/services/retrieval/names.py) | Tra tên địa điểm và phân loại resolved/ambiguous/not_found | Có (SQL) |
| [services/retrieval/candidates.py](../../app/services/retrieval/candidates.py) | Các câu SQL lấy ứng viên | Có (SQL) |
| [services/retrieval/hours.py](../../app/services/retrieval/hours.py) | Giờ mở cửa so với lúc rảnh | Không |
| [services/retrieval/scoring.py](../../app/services/retrieval/scoring.py) | Điểm, lý do, flag cho một địa điểm | Không |
| [services/retrieval/sections.py](../../app/services/retrieval/sections.py) | Chia section, chọn section tốt nhất cho mỗi nơi, đa dạng hoá | Không |
| [services/retrieval/text.py](../../app/services/retrieval/text.py) | Chuẩn hoá chữ, quy tắc dấu tiếng Việt, full-text | Không |
| [services/retrieval/taxonomy.py](../../app/services/retrieval/taxonomy.py) | Cây danh mục `top.sub` và phép "node bao category" | Không |
| [services/retrieval/config.py](../../app/services/retrieval/config.py) + [config/](../../app/services/retrieval/config/) | Nạp và kiểm tra `interest_map`, `keyword_map`, `poi_overrides` | Đọc file 1 lần |
| [services/retrieval/poi.py](../../app/services/retrieval/poi.py) | `Poi`: một dòng bảng `pois` trong bộ nhớ | Không |
| [services/retrieval/db.py](../../app/services/retrieval/db.py) | Tạo pool kết nối Postgres | Không (chỉ cấu hình) |
| [services/retrieval/ingest.py](../../app/services/retrieval/ingest.py) | Dòng parquet → dòng `pois` (chỉ dùng lúc import) | Không |
| [db/schema.sql](../../db/schema.sql) | Bảng `pois` và index | — |
| [scripts/import_parquet.py](../../scripts/import_parquet.py) | Script import | Có |

Các file ghi "Không" ở cột I/O là **hàm thuần**: chỉ nhận dữ liệu và trả dữ liệu, test được không
cần database. Toàn bộ I/O dồn vào `names.py`, `candidates.py` và `service.py`.

## Flow một request

Đây là `retrieve()` trong [service.py](../../app/services/retrieval/service.py), đọc từ trên xuống:

```
POST /retrieval {extraction, selected_ids, section, offset, limit}
│
├─ 0. Validate RetrievalRequest (list ≤ 20, atom ≤ 50) ─────────── sai → 422
├─ 1. get_connection: mượn 1 kết nối từ pool ───────────────────── lỗi → 503
│
├─ 2. Phiếu rỗng hoàn toàn? ──────────────────────────────────────── status=empty_extraction
├─ 3. Điểm đến khác TP.HCM? ──────────────────────────────────────── status=unsupported_destination
│     (null → vẫn chạy, kèm warning "Chưa rõ điểm đến")
│
├─ 4. data_info: danh sách cuisine có trong dữ liệu + data_version   [SQL, cache 5 phút]
├─ 5. plan_interests: mỗi sở thích → kệ / bộ lọc cột / tìm chữ        [thuần]
├─ 6. trip_window: những thứ nào trong tuần, khung giờ nào            [thuần]
│
├─ 7. Tra từng tên (must_visit, preferred_places, avoid)              [SQL × số tên]
│     → pinned / preferred / avoid_ids / ambiguous / not_found
│
├─ 8. keyword_hits cho mỗi sở thích không phải kệ chuẩn               [SQL × 1–2 mỗi sở thích]
├─ 9. fetch_pois: mọi nơi hợp lệ thuộc các kệ + nơi khớp từ khoá      [SQL × 1]
├─ 10. fetch_by_ids: nơi ghim, nơi đã chọn, ứng viên disambiguation   [SQL × 1]
│
├─ 11. assemble: lọc giờ, chấm điểm, chia section, đa dạng hoá       [thuần]
├─ 12. Lọc theo `section` (nếu có), cắt trang offset/limit
└─ 13. Đóng gói response + meta
```

**Số query mỗi request:** 1 (data_info, thường lấy từ cache) + 1 cho mỗi tên (+1 nếu là `avoid`
khớp chính xác) + 1–2 cho mỗi sở thích không chuẩn + 2. Giới hạn list ở bước 0 giữ con số này
có trần. Mọi query chạy **tuần tự trên một kết nối**, vì một kết nối Postgres không chạy song song
được.

## Từng file, theo thứ tự request đi qua

### 1. [routes/retrieval.py](../../app/api/routes/retrieval.py): route

- `get_connection` là dependency dạng **generator** (`yield`): FastAPI mượn kết nối trước khi
  vào hàm route, rồi trả về pool sau khi route xong, kể cả khi route lỗi.
- Pool là `None` (thiếu `DATABASE_URL`) → 503 `retrieval-not-configured`.
- `PoolTimeout` (chờ kết nối quá lâu) hoặc `OperationalError` (database không tới được, query quá
  `statement_timeout`) → 503 `retrieval-db-unavailable` với `Retry-After: 5`.
- Hàm `retrieval` chỉ một dòng: `return await retrieve(conn, body)`.

### 2. [schemas/retrieval.py](../../app/schemas/retrieval.py): request và response

- `RetrievalRequest.extraction` dùng lại đúng model `Extraction` của `/chat`, nên mọi luật strict
  (sai kiểu, thừa field → 422) áp dụng luôn.
- `bounded_lists`: mỗi tên và mỗi sở thích tốn một query, và client tự gửi body, nên phải giới hạn
  (20 phần tử mỗi list, 50 atom, mỗi phần tử hoặc `value` ≤ 200 ký tự: chuỗi dài làm mỗi query
  so tên chậm theo độ dài). Giới hạn đặt ở đây chứ không ở `Extraction`, vì `Extraction` là
  schema gửi cho Gemini và không được đổi.
- `selected_ids` (≤ 50), `section`, `offset`, `limit` (1–20, mặc định 6) dùng cho "xem thêm" và
  xếp hạng theo khoảng cách. Nơi trong `selected_ids` (như nơi ghim) không nằm trong section nào.
- Response: `pinned`, `disambiguation`, `not_found`, `sections` (mỗi section có `items` là
  `PoiCard`), `warnings`, `meta`. Ý nghĩa từng field xem [../API.md](../API.md).

### 3. [service.py](../../app/services/retrieval/service.py): điều phối

Ngoài `retrieve()` (đã mô tả ở flow), có ba hàm phụ:

- `is_empty`: phiếu mọi ô null, mọi list rỗng. Đây là phiếu `/chat` trả cho câu tấn công prompt,
  nên `/retrieval` trả ngay không truy vấn gì.
- `name_card`: thẻ cho nơi tìm theo tên (ghim hoặc ứng viên disambiguation). Nơi ghim **không bị
  loại vì giờ**: người dùng đã nói "phải ghé". Nếu giờ đáng tin nói là đóng, thẻ chỉ có thêm flag
  `hours_closed`. Điểm của thẻ này là độ giống tên, không phải điểm xếp hạng.
- `_page`: cắt `items[offset : offset + limit]`; `total_matches` là tổng trước khi cắt.

Logic của bước tra tên (bước 7), viết ra bằng lời:

| Field | resolved | ambiguous | not_found |
|---|---|---|---|
| `must_visit` | ghim (`pinned`) | `disambiguation` | `not_found` |
| `preferred_places` | cộng điểm thưởng | `disambiguation` | `not_found` |
| `avoid` | loại nơi đó | `disambiguation` | `not_found` |

Riêng `avoid` có thêm luật: nếu có nơi **trùng tên chính xác**, loại **mọi chi nhánh** trùng tên
(`names.exact_ids`, không giới hạn 5 như lookup thường). Nơi vừa bắt buộc vừa cần tránh thì vẫn
giữ ghim, kèm warning.

### 4. [interests.py](../../app/services/retrieval/interests.py): phân loại sở thích

Mỗi giá trị trong `interests`, và mỗi atom `other` có toán tử `include`/`eq`/`prefer`, đi qua
`request_for` và được xét **theo thứ tự, dừng ở bước đầu tiên khớp**:

1. **Sở thích chuẩn** (sau khi đổi alias bằng `name_aliases.csv`) và có trong `interest_map` →
   `InterestRequest(group=...)`, tức là một kệ theo taxonomy.
2. **Từ khoá có cấu trúc**: tên đã `norm()` là alias trong `keyword_map`, **hoặc** trùng một giá
   trị `cuisine` đang có trong dữ liệu (`data_info`) → `InterestRequest(keyword=...)`, tức là bộ
   lọc theo cột `michelin`/`cuisine`/`features`. Nếu keyword trỏ sang một group (ví dụ "bar" →
   `nightlife`) thì thành kệ như bước 1.
3. **Còn lại** → `InterestRequest(text=norm(value))`, tức là tìm chữ (full-text).

Ranh giới giữa bước 2 và 3 phụ thuộc **dữ liệu**: một giá trị cuisine mới xuất hiện sau lần import
sẽ tự chuyển từ bước 3 sang bước 2, không cần sửa code.

Các chi tiết khác:

- **Mức độ** (`_strength`): sở thích có atom hard `include`/`eq` trùng giá trị → `include` (bắt
  buộc); còn lại → `prefer`. Atom `other` trong `hard_constraints` → `include`, trong
  `soft_constraints` → `prefer`.
- **Gộp trùng** theo `key` (tên kệ, `kw:<keyword>` hoặc `kw:<chữ>`). Hai yêu cầu cùng key thì giữ
  một, và `include` thắng `prefer`.
- **`avoid`** đi qua cùng hai bước đầu, nhưng kết quả khác:
  - Bước 1 hoặc group → thêm các node taxonomy vào `avoid.nodes` (ví dụ "bar" →
    `entertainment.nightlife`, loại cả nhánh).
  - Bước 2 dạng bộ lọc → `avoid.filters` (ví dụ "chay" → loại nơi có cuisine chay).
  - Không khớp gì → `avoid.names`, đem đi **tra tên** chứ không tìm chữ.
- Atom `other` hard `exclude` cũng thành luật tránh, nhưng chỉ khi nó khớp từ khoá có cấu trúc.

Output thật với `interests=["cafe","phở","michelin"]`, `avoid=["bar"]`:

```
req cafe     prefer group=cafe     key=cafe
req phở      prefer text=pho       key=kw:pho
req michelin prefer keyword=michelin key=kw:michelin
avoid: nodes={'entertainment.nightlife'}, filters=(), names=()
```

### 5. [config.py](../../app/services/retrieval/config.py) và thư mục [config/](../../app/services/retrieval/config/)

- `config.py` nạp ba file **một lần khi import module** và kiểm tra hình dạng: node taxonomy đúng
  dạng `top.sub`, keyword chỉ lọc các cột cho phép, mỗi keyword có đúng một trong `filter`/`group`,
  không alias nào trỏ tới hai keyword. Sai → app không khởi động được, thay vì lỗi giữa request.
- `interest_map.v2.yaml`: kệ chuẩn → danh sách node taxonomy, nhãn hiển thị, `defaults` (kệ hiển
  thị khi không có sở thích nào: `food`, `culture`), `taxonomy_gaps` (node không có trong taxonomy do lỗi
  dữ liệu đã biết; hiện trống).
- `keyword_map.v1.yaml`: alias (đã `norm`) → bộ lọc cột hoặc group; `cuisine_labels` để hiển thị
  lý do bằng tiếng Việt.
- `poi_overrides.csv`: sửa `category`/`kind` theo id, **chỉ dùng lúc import**.
- `Group.node_for(category)`: node **sâu nhất** của kệ bao category đó. Dùng để quyết định kệ nào
  cụ thể hơn khi hai kệ cùng nhận một nơi.

### 6. [taxonomy.py](../../app/services/retrieval/taxonomy.py): cây danh mục

- Category của một nơi luôn có dạng `top.sub` (ví dụ `food.cafe`). Một **node** là `top` (cả
  nhánh: `food` bao `food.cafe`, `food.restaurant`...) hoặc một `sub` cụ thể.
- `covers(node, category)`: node có bao category không. `depth(node)`: 1 cho top, 2 cho sub.
- Nhờ cách này, một sub mới thêm vào dưới `food` tự động vào kệ "Ẩm thực" mà không sửa code.

### 7. [text.py](../../app/services/retrieval/text.py): chữ và dấu tiếng Việt

Đây là cơ chế khó nhất. Vấn đề: bỏ dấu làm nhiều từ khác nghĩa trùng nhau ("phở"/"phố",
"bò"/"bộ"). Mẫu dữ liệu có khoảng 7% từ bị trùng như vậy.

**Hai dạng chuẩn hoá:**

| Hàm | Làm gì | `"Phở Hòa Pasteur"` → |
|---|---|---|
| `norm()` | thường hoá, **bỏ dấu** (đ→d), dấu câu → khoảng trắng | `pho hoa pasteur` |
| `fold_case()` | thường hoá, **giữ dấu**, dấu câu → khoảng trắng | `phở hòa pasteur` |

**Quy tắc dấu**, áp dụng cho từng từ:

- Từ người dùng gõ **có dấu** chỉ khớp đúng từ đó, hoặc từ trong dữ liệu được viết **không dấu**
  ("phở" khớp "phở" và "pho", không khớp "phố").
- Từ gõ **không dấu** không mang thông tin dấu, nên khớp mọi kiểu dấu ("pho" khớp cả "phở" và
  "phố").

**Cách đưa quy tắc vào Postgres full-text** (không cần danh sách từ):

- Lúc import, `lexeme_vector` lưu mỗi từ ở vị trí của nó với **weight A** (như được viết), và nếu
  bỏ dấu làm từ thay đổi thì lưu thêm dạng bỏ dấu ở **cùng vị trí** với **weight B**. Output thật
  cho `["Phở Hòa", "phố cổ"]`:
  ```
  'phở':1A  'pho':1B,13B  'hòa':2A  'hoa':2B  'phố':13A  'cổ':14A  'co':14B
  ```
  (Vị trí nhảy từ 2 lên 13 là `TEXT_GAP = 10` giữa hai đoạn văn bản, để một cụm từ không khớp vắt
  qua hai đoạn.)
- Lúc request, `lexeme_query` dựng câu truy vấn. Output thật:
  ```
  "phở bò" → ('phở':A | 'pho':A) <-> ('bò':A | 'bo':A)
  "pho"    → 'pho':AB
  ```
  - `'phở':A`: từ "phở" được viết có dấu trong dữ liệu.
  - `'pho':A`: từ "pho" được **viết không dấu** trong dữ liệu (weight A = như được viết).
  - "phố" chỉ sinh ra `'pho'` ở **weight B**, nên không khớp `'pho':A`. Đó chính là quy tắc dấu.
  - Query không dấu `'pho':AB` chấp nhận cả A lẫn B, nên khớp mọi kiểu dấu.
  - `<->` nghĩa là "từ kế tiếp", nên nhiều từ phải xuất hiện liền nhau như một cụm.

### 8. [names.py](../../app/services/retrieval/names.py): tra tên

**SQL `LOOKUP_SQL`** chạy trên cột `name_keys`, là mảng các dạng `norm()` của tên, tên tiếng Anh,
tên ASCII và các tên gọi khác:

- Với mỗi nơi, mỗi khoá `k`: độ giống = max của `similarity(k, q)` (giống toàn chuỗi, theo
  trigram) và `word_similarity(q, k)` (query giống một đoạn trong tên; giúp "Ben Thanh" khớp
  "Chợ Bến Thành").
- `exact` = có khoá nào bằng đúng query; `exact_marked` = query có dấu và trùng dạng giữ dấu.
- Giữ nơi khớp chính xác hoặc có độ giống ≥ 0.35, xếp chính xác trước rồi theo độ giống, lấy 5.

**`classify`** (thuần) quyết định:

1. Có nơi khớp chính xác. Nếu query có dấu, ưu tiên nơi khớp cả dấu ("Phở Hùng" thắng "Phố
   Hùng"). Còn đúng một nơi → `resolved`; nhiều nơi (chuỗi cửa hàng) → `ambiguous`.
2. Không có khớp chính xác:
   - Nơi giống nhất < 0.35 → `not_found`.
   - Nơi giống nhất ≥ 0.6 **và** hơn nơi thứ hai ≥ 0.15 → `resolved`.
   - Còn lại → `ambiguous`, kèm mọi nơi trong khoảng 0.15 dưới nơi giống nhất.

Output thật:

```
"Phở Hùng", hai nơi khớp chính xác, chỉ một nơi khớp cả dấu → resolved (nơi khớp dấu)
"Ben Thanh", độ giống 0.72 và 0.41                            → resolved (0.72, cách 0.31)
"Ben",       độ giống 0.50 và 0.45                            → ambiguous (cả hai)
```

Các ngưỡng là hằng số có tên ở đầu file (`RESOLVE_MIN`, `RESOLVE_GAP`, `AMBIGUOUS_MIN`,
`AMBIGUOUS_BAND`, `LOOKUP_LIMIT`), là giá trị khởi điểm, chỉnh bằng `eval/run_eval.py`.

**`exact_ids`** (chỉ dùng cho `avoid`): `WHERE name_keys @> ARRAY[q]` dùng GIN index
`pois_name_keys_idx`, không có LIMIT, rồi áp cùng quy tắc dấu qua `classify`.

### 9. [candidates.py](../../app/services/retrieval/candidates.py): SQL lấy ứng viên

- **`ELIGIBLE`**: điều kiện chung cho mọi truy vấn ứng viên: `recommendable`, không nằm ngoài
  vùng, không nằm trong `avoid_ids`, category không thuộc node bị tránh.
- **`SUGGESTABLE`**: ngưỡng chất lượng `confidence ≥ MIN_CONFIDENCE` (0.7) cho ứng viên theo kệ và
  hit từ khoá. Nơi người dùng gọi tên (`preferred_places`, `must_visit`) không qua ngưỡng này.
- **`in_nodes`**: "category thuộc một trong các node". Node sub so với `category`, node top so
  với `category_top` (cột sinh tự động trong schema). Cả hai cột đều có index.
- **`keyword_hits`** cho mỗi sở thích không phải kệ chuẩn, trả `{poi_id: loại bằng chứng}`:
  - Keyword có bộ lọc → `STRUCTURED_SQL` (`michelin = ANY`, `cuisine && ...`, `features && ...`;
    `&&` là "hai mảng có phần tử chung"). Bằng chứng `structured`.
  - Sở thích tìm chữ (hoặc keyword có `text: true`, ví dụ "bánh mì") → `TEXT_SQL`. Tìm bằng
    **chữ gốc người dùng gõ** (`request.raw`), không phải chữ đã bỏ dấu, để giữ quy tắc dấu. Khớp
    trong tên (`name_lex`, hoặc `word_similarity` ≥ 0.8 trên tên giữ dấu để chịu lỗi gõ) → bằng
    chứng `name`; chỉ khớp ở cuisine/features/mô tả (`search_lex`) → bằng chứng `text`.
  - Một nơi khớp cả hai cách thì giữ bằng chứng mạnh hơn (`structured` được ghi trước).
- **`fetch_pois`**: lấy mọi nơi hợp lệ thuộc các node của kệ được yêu cầu, cộng các id khớp từ khoá
  và id `preferred`. Đọc đúng các cột trong `POI_COLUMNS`, đổi thành `Poi`.
- **`fetch_by_ids`**: lấy theo id **không** qua `ELIGIBLE`. Nơi người dùng đã chọn hoặc bắt buộc
  thì luôn hiện, kể cả khi bị đánh dấu không gợi ý.
- **`data_info`**: một query lấy danh sách cuisine riêng biệt và `data_version`, cache trong biến
  module 5 phút. Hai giá trị này chỉ đổi khi import lại.

### 10. [hours.py](../../app/services/retrieval/hours.py): giờ mở cửa

**Mô hình:** một tuần là một vòng tròn 10080 phút (thứ Hai 00:00 = 0). Giờ mở cửa và lúc người
dùng rảnh đều đổi thành các khoảng phút trên vòng này.

- `weekly_intervals`: `{"mon": [["08:00","22:00"]], ...}` → các khoảng. Giờ đóng nhỏ hơn giờ mở
  nghĩa là **qua nửa đêm** (sang ngày sau); mở = đóng nghĩa là 24 giờ; Chủ nhật qua nửa đêm vòng
  về thứ Hai (`_wrap`).
- `trip_weekdays`: từ `start_date`/`end_date`/`duration_days` suy ra những thứ trong tuần của
  chuyến đi (tối đa 7, vì sau 7 ngày thì lặp lại). Không có ngày nào → `None` (mọi thứ).
- `TripWindow.day_window`: khung giờ rảnh của một thứ; `22:00–02:00` cũng chạy qua nửa đêm.
  `active=False` khi phiếu không nói gì về ngày lẫn giờ, khi đó **không lọc giờ**.
- `check_hours`: một nơi coi là **mở** vào một thứ nếu **có giao** với khung rảnh (không cần mở
  suốt cả khung). `fit` = tỉ lệ số thứ mở trên số thứ của chuyến đi. Không có giờ → `unknown`.
- `decide`: chỉ loại khi giờ **đáng tin** (confidence `medium`/`high` và không mâu thuẫn giữa các
  nguồn):

  | Kết quả kiểm tra | Giờ đáng tin | Giờ không đáng tin |
  |---|---|---|
  | `closed` | **loại** | giữ, flag `hours_maybe_closed` |
  | `open` | giữ | giữ, flag `hours_low_confidence` |
  | `unknown` | giữ, flag `hours_unknown` | giữ, flag `hours_unknown` |

  Có mâu thuẫn giữa các nguồn → thêm flag `hours_contradicted`.

Output thật: chuyến đi bắt đầu 2026-11-21 (thứ Bảy), 2 ngày, rảnh 18:00–22:00 →
`weekdays=(5, 6)` (thứ Bảy, Chủ nhật), `start=1080`, `end=1320` (phút trong ngày). Một quán chỉ mở
06:00–11:00 → `closed`. Confidence `high` → **loại**; confidence `low` → giữ với
`hours_maybe_closed`.

### 11. [scoring.py](../../app/services/retrieval/scoring.py): điểm, lý do, flag

**Điểm = độ phù hợp (trung bình có trọng số của các phần có mặt) + 0 × `quality` + 0.06 ×
`explore`, + 0.25 nếu là nơi người dùng thích.** `quality` cố định theo từng nơi, nên nếu là điểm
xếp hạng thì nơi nổi tiếng luôn thắng; với trọng số > 0, dù chỉ 0.01, 3 vị trí đầu kệ ẩm thực
luôn là tier A (places_1000). Hai phần cộng thêm đều nhỏ hơn nhiều so với một bậc bằng chứng
(khớp tên so với category lệch ~0.17), nên tín hiệu thật luôn thắng.

| Phần | Trọng số | Cách tính | Có mặt khi |
|---|---|---|---|
| `match` | 0.55 | Bằng chứng mạnh nhất × mức độ. `structured` 1.0, `name` 0.8, `category` 0.6, `text` 0.5; × 1.0 nếu bắt buộc, × 0.85 nếu ưu tiên | luôn |
| `quality` | cộng 0 | 0.45 × tier (A 1.0, B 0.6, C 0.3) + 0.25 × min(số nguồn giới thiệu, 4)/4 + 0.15 × Michelin (sao 1.0, Bib 0.8, Selected 0.5) + 0.15 × confidence | luôn |
| `time_fit` | 0.15 | `fit` của kiểm tra giờ; 0 nếu `hours_maybe_closed`; 0.3 nếu không biết giờ | phiếu có ngày/giờ |
| `proximity` | 0.15 | `exp(−km / 2)` tới nơi gần nhất trong số nơi đã ghim/đã chọn (khoảng cách haversine) | có nơi ghim hoặc `selected_ids` |
| `explore` | cộng 0.06 | 4 byte đầu của `sha256("seed:id")` quy về [0, 1). Seed = `request.seed`, không có thì lấy từ hash `extraction` (`service.request_seed`) | luôn |

"Trung bình của các phần có mặt" nghĩa là phần vắng mặt không kéo điểm xuống: không có ngày giờ
thì độ phù hợp chỉ là `match`.

**`reasons`** chỉ ghép từ giá trị đã lưu: nhãn Michelin, "Khớp “phở”" / "Mô tả có “phở”", nhãn
cuisine, "Có món chay", giờ mở của thứ phù hợp, "Được N nguồn giới thiệu", giá. Không có câu nào
do AI sinh, nên mọi lý do đều kiểm chứng được.

**`card_flags`**: các flag giờ, cộng `no_price` khi không có giá.

### 12. [sections.py](../../app/services/retrieval/sections.py): chia section

`assemble()` là bước thuần lớn nhất. Các bước:

**a. Tìm "nhà" của từ khoá và tỉa bớt hit** (`keyword_home`, `prune_hits`):

- Đếm hit của từ khoá theo category, có trọng số: hit `name`/`structured` tính 1, hit chỉ ở mô tả
  (`text`) tính 0.3.
- **Nhà** = category chiếm ≥ 60% tổng trọng số; nếu không có thì thử theo top level; không có nữa
  thì không có nhà.
- Hit chỉ ở mô tả mà **nằm ngoài nhà** bị bỏ. Ví dụ: một khu chợ có mô tả nhắc tới phở không phải
  là quán phở.

**b. Dựng section** (`build_specs`):

- Mỗi sở thích dạng kệ → một section. Không có sở thích nào → dùng `defaults`. Kệ không có node
  nào (ví dụ `beach` ở TP.HCM) → warning.
- Mỗi sở thích dạng từ khoá:
  - Không có hit → warning; nếu là sở thích bắt buộc thì vẫn tạo section rỗng có `note`. Atom
    `other` không có hit thì bỏ im lặng, vì nó có thể không phải là địa điểm ("đi cùng trẻ em").
  - Có kệ được yêu cầu bao nhà của nó → **gộp** vào kệ đó (`matched_keywords`), nơi khớp được đẩy
    lên đầu kệ.
  - Không có → section riêng, key `kw:<...>`.

**c. Đặt mỗi nơi vào đúng một section** (`_best_placement`):

- Bỏ nơi đã ghim (đã hiện ở `pinned`) và nơi bị luật tránh (`avoided`, gồm cả bộ lọc cột như "tránh
  đồ chay", bộ lọc này không làm được trong SQL `ELIGIBLE`).
- Kiểm tra giờ một lần; giờ đáng tin nói đóng → loại.
- Chấm điểm nơi đó ở **mọi** section nhận nó, giữ section điểm cao nhất. Hoà thì section cụ thể hơn
  thắng (node sâu hơn, rồi kệ ít node hơn; section từ khoá riêng là cụ thể nhất), rồi section bắt
  buộc, rồi thứ tự người dùng nêu. Vì vậy một quán cà phê nằm trong "Cà phê" chứ không nằm trong
  "Ẩm thực" khi người dùng yêu cầu cả hai.
- Nơi `preferred` không vào section nào → section riêng `preferred` ("Địa điểm bạn thích").

**d. Sắp xếp trong section** (`order_items`, `diversify`):

- Ba tầng: nơi người dùng thích → nơi khớp từ khoá đã gộp → còn lại. Trong mỗi tầng xếp theo điểm.
- Đa dạng hoá tham lam trên 48 nơi đầu: trong mỗi cửa sổ 8 nơi, tối đa 2 nơi cùng cuisine chính, 4
  nơi cùng phường, 3 nơi nổi tiếng (tier A hoặc Michelin). Section do Michelin dẫn dắt không bị
  giới hạn nơi nổi tiếng. Cuisine
  quá phổ biến (`vietnamese`, `diner`, `asian`, `regional`) không bị giới hạn. Không nơi nào thoả
  thì vẫn lấy nơi điểm cao nhất, nên không bao giờ mất kết quả.

**e. Hoàn tất** (`_finish`): section rỗng thì bỏ, trừ khi bắt buộc (giữ kèm `note`); từ khoá đã
gộp mà không khớp nơi nào trong kệ → warning.

`to_card` đổi `Scored` thành `PoiCard` của response.

### 13. [db.py](../../app/services/retrieval/db.py): pool kết nối

- Mỗi instance serverless giữ một pool nhỏ (`max_size` mặc định 3), bắt đầu rỗng (`min_size=0`).
  Serverless có nhiều instance sống ngắn; pool lớn ở mỗi instance sẽ làm cạn kết nối Postgres.
- `timeout`: thời gian tối đa chờ một kết nối rảnh.
- `autocommit=True`: chỉ có truy vấn đọc, không cần giữ transaction mở.
- `prepare_threshold=None` khi đi qua PgBouncer chế độ transaction (cổng 6432 của Azure). PgBouncer
  có thể đưa mỗi transaction sang một kết nối server khác, nên prepared statement phía server sẽ
  hỏng.
- `options`: `search_path=<schema>,public` (chọn schema dữ liệu, ví dụ `retrieval_test`) và
  `statement_timeout` (giết query chạy quá lâu kể cả khi function đã bị Vercel tắt). Tên schema
  được kiểm tra bằng regex vì nó bị ghép thẳng vào chuỗi `options`.

### 14. [poi.py](../../app/services/retrieval/poi.py): bản ghi trong bộ nhớ

`POI_COLUMNS` là danh sách cột duy nhất mà mọi `SELECT` dùng; `Poi.from_row` đổi dòng SQL thành
dataclass bất biến (mảng → tuple). `kind` chỉ để hiển thị; section dùng `category`.

### 15. Phía import: [ingest.py](../../app/services/retrieval/ingest.py), [schema.sql](../../db/schema.sql), [import_parquet.py](../../scripts/import_parquet.py)

Không chạy lúc request, nhưng tạo ra mọi cột mà request đọc:

| Cột | Tính từ | Dùng ở |
|---|---|---|
| `name_keys` | `norm()` của tên, tên EN, tên chuẩn hoá, tên ASCII, tên gọi khác | `names.LOOKUP_SQL`, `EXACT_SQL` |
| `name_keys_vi` | `fold_case()` của tên **như được viết** (không dùng tên ASCII, vì nó đã mất dấu và sẽ làm "Phố X" trông như "pho" viết không dấu) | `exact_marked` |
| `name_text` | `name_keys_vi` nối lại | typo check trong `TEXT_SQL` |
| `name_lex` | `lexeme_vector` của tên như được viết | hit `name` trong `TEXT_SQL` |
| `search_lex` | `lexeme_vector` của tên, cuisine, features, nhãn Michelin, mô tả | hit `text` trong `TEXT_SQL` |
| `category_top` | Postgres tự sinh: phần trước dấu chấm của `category` | `in_nodes` |
| `opening_hours` | `readable_hours`: giữ nếu đọc được, không thì `NULL` (coi là không biết giờ) | `hours.py` |
| `category`, `kind` | dữ liệu gốc, sửa theo `poi_overrides.csv` | sections |

Script import: upsert theo `id` (chạy lại an toàn), `--prune` xoá id không còn trong file,
`--dry-run` chỉ biến đổi và in tóm tắt, kiểm tra category với taxonomy (lỗi, hoặc chỉ warning nếu
`--no-strict-taxonomy`), in cảnh báo cho giờ mở cửa không đọc được.

## Đọc code theo thứ tự nào

1. [service.py](../../app/services/retrieval/service.py) `retrieve()`: nắm khung tổng thể.
2. [interests.py](../../app/services/retrieval/interests.py): sở thích được hiểu thế nào.
3. [text.py](../../app/services/retrieval/text.py), rồi [names.py](../../app/services/retrieval/names.py): khớp chữ và tên.
4. [candidates.py](../../app/services/retrieval/candidates.py): SQL thật sự chạy.
5. [hours.py](../../app/services/retrieval/hours.py) → [scoring.py](../../app/services/retrieval/scoring.py) → [sections.py](../../app/services/retrieval/sections.py): xếp hạng.
6. Test tương ứng trong `tests/test_retrieval_*.py`: mỗi hàm thuần đều có ví dụ input/output.

## Giới hạn đã biết

- **Chỉ TP.HCM.** Điểm đến khác trả `unsupported_destination`.
- **`fetch_pois` nạp mọi nơi hợp lệ của các kệ vào Python** mỗi request và mỗi trang "xem thêm",
  vì giờ và điểm tính bằng Python. Ổn dưới khoảng 10k nơi hợp lệ; vượt ngưỡng thì cần xếp hạng sơ
  bộ trong SQL và LIMIT theo node (có comment `shortcut:` trong code).
- **Khi chỉ có sở thích dạng từ khoá** (ví dụ chỉ "phở"), `service.py` vẫn nạp các nơi thuộc kệ
  mặc định (`food`, `culture`), dù không section nào dùng chúng. Kết quả vẫn đúng, chỉ tốn công đọc
  thêm.
- **`LOOKUP_SQL` quét mọi dòng** (tính trigram trên từng tên), không dùng index. Ổn với dữ liệu
  hiện tại; dữ liệu lớn lên thì cần chuyển sang toán tử có index của `pg_trgm`.
- **Cache `data_info` theo từng process**: sau khi import, mỗi instance có thể thấy dữ liệu cũ tối
  đa 5 phút.
- **Lọc `section` làm sau khi đã tính toàn bộ**: "xem thêm" một section vẫn tốn công như request
  đầy đủ.
- **Các ngưỡng và trọng số** (0.6/0.15/0.35, 0.55/0.15/0.15, quality 0 / explore 0.06, confidence 0.7, 60%/0.3, caps
  đa dạng) là giá
  trị khởi điểm, chưa được hiệu chỉnh trên dữ liệu thật lớn.
- **Cấu hình từ vựng** (`keyword_map`, `poi_overrides`) còn liệt kê tay. Xem thảo luận về việc
  chuyển chúng sang dữ liệu trước khi mở rộng.
