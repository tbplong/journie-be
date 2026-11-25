# `POST /retrieval`: giải thích code

## ELI5

`/chat` đã điền xong **phiếu yêu cầu** (`extraction`). `/retrieval` là người thủ thư cầm phiếu đi
tìm trong sổ địa điểm của đúng thành phố đó (TP.HCM, Hà Nội, Đà Nẵng hoặc Huế):

1. **Tìm theo tên**: phiếu ghi "phải ghé Dinh Độc Lập" → tra đúng nơi đó, ghim lên đầu. Tên mơ hồ
   (nhiều nơi giống nhau) → hỏi lại "ý bạn là nơi nào?". Không thấy → báo không thấy, **không bao
   giờ tự đổi sang một nơi "na ná"**.
2. **Tìm theo sở thích**: "thích cà phê" → lấy kệ "Cà phê". "Thích phở" không có kệ riêng → tìm
   chữ "phở" trong tên và mô tả, rồi xếp kết quả vào kệ phù hợp nhất. Câu tự do ("chỗ có view
   đẹp để ngắm hoàng hôn") còn được tìm **theo nghĩa**: so embedding của câu với embedding mô tả
   từng nơi.
3. **Gạch bỏ**: nơi cần tránh, nơi đóng cửa vào lúc người dùng rảnh.
4. **Chấm điểm và xếp hàng**: nơi khớp tốt, mở cửa đúng giờ, gần chỗ đã chọn thì lên trước. Chất
   lượng chỉ là ngưỡng phải đạt, không phải điểm xếp hạng; các nơi phù hợp ngang nhau được xáo theo
   `seed`. Mỗi kệ không để một kiểu quán (hay nơi nổi tiếng) chiếm hết chỗ.
5. **Giải thích**: mỗi gợi ý kèm lý do lấy từ dữ liệu ("Michelin Bib Gourmand", "Mở 06:00–22:00
   thứ Bảy"), không có câu nào do AI viết.

Không có LLM sinh chữ ở đây. Ngoài SQL và Python thuần chỉ có một lần gọi Gemini để embed các câu
tự do (`semantic.py`); embedding của cùng một câu không đổi, nên cùng một phiếu cho cùng một kết
quả. Không có embedder (thiếu key, lỗi, hết quota) thì chỉ còn tìm theo chữ.

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
| [services/retrieval/semantic.py](../../app/services/retrieval/semantic.py) | Tìm theo nghĩa: định dạng câu embed, ngưỡng tương đồng, phủ định, cache câu | Có (SQL) |
| [services/gemini.py](../../app/services/gemini.py) | Gọi Gemini (`embed_texts`, đổi key khi gặp 429) | Có (HTTP) |
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
├─ 3. Điểm đến ngoài 4 thành phố? ──────────────────────────────── status=unsupported_destination
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
│     (trả kết nối về pool)
├─ 8b. _semantic: embed tối đa 5 câu tự do (1 lần gọi Gemini, cache), [Gemini × 0–1]
│     rồi mượn lại kết nối, tìm nơi gần nghĩa cho từng câu            [SQL × số câu]
├─ 9. fetch_pois: mọi nơi hợp lệ thuộc các kệ + nơi khớp từ khoá      [SQL × 1]
├─ 10. fetch_by_ids: nơi ghim, nơi đã chọn, ứng viên disambiguation   [SQL × 1]
│
├─ 11. assemble: lọc giờ, chấm điểm, chia section, đa dạng hoá       [thuần]
├─ 12. Lọc theo `section` (nếu có), cắt trang offset/limit
└─ 13. Đóng gói response + meta
```

**Số query mỗi request:** 1 (data_info, thường lấy từ cache) + 1 cho mỗi tên (+1 nếu là `avoid`
khớp chính xác) + 1–2 cho mỗi sở thích không chuẩn + 1 cho mỗi câu tìm theo nghĩa (tối đa 5) + 2,
và tối đa 1 lần gọi Gemini embed. Giới hạn list ở bước 0 giữ con số này
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
`wish_requests` theo thứ tự:

0. **Từ chỉ thời gian** (`ignored_wishes` trong `keyword_map`: "tối nay", "ngày mai", "cuối
   tuần", "tonight"...): cả giá trị là một từ như vậy (so sau `norm()`) → không tạo yêu cầu nào,
   với mọi toán tử, kể cả trong `interests`.
1. **Khớp nguyên giá trị** (`exact_requests`), dừng ở bước đầu tiên khớp:
   1. **Sở thích chuẩn** (sau khi đổi alias bằng `name_aliases.csv`) và có trong `interest_map` →
      `InterestRequest(group=...)`, tức là một kệ theo taxonomy.
   2. **Từ khoá có cấu trúc**: tên đã `norm()` là alias trong `keyword_map`, **hoặc** trùng một
      giá trị `cuisine` đang có trong dữ liệu (`data_info`) → `InterestRequest(keyword=...)`, tức
      là bộ lọc theo cột `michelin`/`cuisine`/`features`. Keyword trỏ sang một group ("bar" →
      `nightlife`) thì thành kệ như bước 1; nếu keyword đó có `text: true` ("pub", "bia",
      "nhậu", nhóm `nightlife_kind`) thì thêm một yêu cầu tìm chữ đi kèm group
      (`InterestRequest.companion`, luôn `source="other"`) cho chính chữ người dùng gõ, bỏ các từ
      đệm đứng đầu (`filler_words`): "đi nhậu" tìm "nhậu", vì không mô tả nào viết "đi nhậu" mà
      các quán nhậu ở Hà Nội vẫn phải có section riêng. Gõ không dấu thì tìm alias như được viết
      (`_kind_words`): "nhau", "di nhau" tìm "nhậu", vì "nhau" không dấu khớp "cùng nhau" trong
      mọi mô tả.
   3. **Còn lại** → `InterestRequest(text=norm(value))`, tức là tìm chữ (full-text và theo nghĩa).
2. Với một yêu cầu tìm chữ:
   - `other eq` nêu **sự thật** (`is_fact`) → bỏ: một số tiền ("200k", "$75", "1.5tr"), một giờ
     ("9h30", "20:40"), một tên tiền tệ đứng cạnh một số ("75 NZD", "2 triệu", "75 đô"; so như
     được viết, nên "đồ" không phải "đô"), hoặc một số đi cùng từ ngân sách ("budget 75"). Tên
     tiền tệ đứng một mình là chữ chỉ nơi chốn ("khám phá cố đô", "dạo quanh thủ đô", "khu đô
     thị"), số đứng một mình cũng vậy ("ăn ở quận 1", "cà phê 24h"). Mọi `other eq` khác là ước
     muốn: /chat hay ghi "kiếm chỗ uống rượu" là `eq`.
   - Từ chỉ thời gian **nằm trong** câu bị cắt ra rồi đọc lại phần còn lại ("tối nay đi nhậu" →
     "đi nhậu"), để section không mang tên giờ giấc.
   - Câu **chứa** một alias của group hoặc của sở thích chuẩn (`contained_group`, so trọn từ) →
     thêm yêu cầu group đó trước yêu cầu tìm chữ: "kiếm chỗ uống rượu" → `nightlife` + chữ "kiếm
     chỗ uống rượu". Yêu cầu tìm chữ đó thành chữ đi kèm group (`companion`, `source="other"`
     dù atom là `interest`): group trả lời ước muốn, nên tìm chữ không ra gì (hoặc không có
     embedder) thì không có warning "Không tìm thấy". Phủ định **đứng trước** alias là từ chối
     ("không uống rượu", "tránh xa bar", "don't want a bar") → không định tuyến; phủ định đứng
     **sau** alias chỉ tả nơi đó ("quán bar không ồn ào", "quán bar ko ồn") → vẫn ra
     `nightlife` (`semantic.first_negation`). Alias dài nhất thắng, nhưng group có dữ
     liệu thắng alias dài hơn của group chưa có dữ liệu: "beach bar", "bar ven biển", "bia ven
     biển" là bar, vì không có bãi biển nào để hiện. Luật dấu ở đây chặt: từ gõ có dấu phải bằng
     đúng từ của alias như được viết, từ không dấu chỉ khớp alias có dấu khi **cả câu** không dấu
     ("kiem cho uong ruou" vẫn chứa "uống rượu", "bìa rừng" không chứa "bia", "cùng nhau" không
     chứa "nhậu"). Cụm trong `not_aliases` ("bia tiến sĩ", "bắn bia", "cơm rượu", "nấu rượu",
     "cùng nhau", "với nhau"...) bị che trước khi tìm; trong câu gõ không dấu chúng che cả cách
     viết không dấu, nên "di choi cung nhau" và "bia rung" không ra bar, còn "đi nhậu với nhau"
     vẫn ra. Group không có node (`beach`): nếu phần còn lại của câu chỉ là từ đệm
     (`filler_words`: "bãi biển", "đi biển", "đi tắm biển", "go to the beach") thì chỉ giữ yêu
     cầu group và warning không có dữ liệu (quán tên "Biển" không phải bãi biển). Câu có thêm ý
     ("ăn hải sản ven biển", "nhà hàng ven biển") vẫn tìm chữ; warning không có dữ liệu chỉ hiện
     khi cả phần tìm chữ cũng không ra nơi nào.

Ranh giới giữa bước 1.2 và 1.3 phụ thuộc **dữ liệu**: một giá trị cuisine mới xuất hiện sau lần import
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
  Atom `other` có toán tử `exclude`/`min`/`max` không bao giờ là ước muốn.

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
- `keyword_map.v1.yaml` (bản `keyword_map.v1.2`, ghi trong `meta.keyword_map_version`): alias →
  bộ lọc cột hoặc group. Alias được tra sau `norm()`, nhưng cũng được giữ **như được viết**
  (`Keyword.written`) để tìm alias nằm trong câu theo luật dấu chặt, nên alias của group phải viết
  đủ dấu. `nightlife` (bar, rượu, uống rượu...) chỉ cho cả nhóm; `nightlife_kind` (pub, bia,
  nhậu, cocktail, rượu vang, craft beer...) cho cả nhóm và tìm thêm theo chữ (`text: true`).
  Cả hai có dạng số nhiều và cụm tiếng Anh ("bars", "drinks", "grab a drink", "pubs",
  "cocktails", "rooftop bars", "craft beers"), vì /chat giữ nguyên sở thích ngoài danh sách chuẩn
  như người dùng viết.
  `ignored_wishes`: từ chỉ thời gian, không phải sở thích (không được trùng alias nào).
  `not_aliases`: cụm chứa alias mà nghĩa khác ("bia tiến sĩ"). `filler_words`: từ đệm một chữ
  ("đi", "bãi", "tắm", "muốn", "go", "the"...) cho luật bãi biển ở trên và cho chữ đi kèm của
  `text: true`. `cuisine_labels` để hiển thị lý do bằng tiếng Việt.
- `load_interest_aliases()` đọc tên và alias của 8 sở thích chuẩn như được viết từ
  `extraction/name_aliases.csv` (`NAME_ALIASES` bỏ khoảng trắng nên không dùng để tìm cụm từ).
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
- **Dạng chặt** (`lexeme_query(text, strict=True)`), dùng cho `search_lex` (mô tả, cuisine,
  features): từ có dấu chỉ khớp chính nó như được viết, không khớp dạng viết không dấu. Output
  thật:
  ```
  "nhậu"   → 'nhậu':A        (dạng thường: ('nhậu':A | 'nhau':A))
  "chợ"    → 'chợ':A         (dạng thường: ('chợ':A | 'cho':A))
  "phở bò" → 'phở':A <-> 'bò':A
  "pho"    → 'pho':AB        (không dấu: như dạng thường)
  ```
  Tên quán hay được viết không dấu ("Pho Hoa"), nên `name_lex` giữ dạng thường. Mô tả thì viết đủ
  dấu, và chữ không dấu trong mô tả thường là một từ khác: trong `retrieval_test` có 68 mô tả
  TP.HCM chứa "nhau" mà chỉ 1 mô tả chứa "nhậu", 458 chứa "cho" và 26 chứa "chợ"; dạng thường
  từng cho "nhậu" ra 35 nhà hàng và "chợ" ra 349 nơi.
- **Hai cách đặt dấu thanh** (`tone_variant`): vần mở tận cùng bằng "oa", "oe", "uy" được viết
  theo cả kiểu cũ lẫn kiểu mới ("hòa"/"hoà", "khỏe"/"khoẻ", "thủy"/"thuỷ"; trong mô tả có cả
  hai). Ở cả hai dạng truy vấn, một từ có dấu khớp thêm cách đặt dấu kia; chỉ đổi phía truy vấn,
  nên không phải dựng lại `search_lex` hay `name_lex`. Output thật:
  ```
  "điều hoà" (chặt) → 'điều':A <-> ('hoà':A | 'hòa':A)        16 mô tả, trước đây 0
  "thủy cung"       → ('thủy':A | 'thuỷ':A | 'thuy':A) <-> 'cung':AB
  ```
  Vần có phụ âm cuối hay nguyên âm cuối ("hoàng", "ngoài"), "qu" ("quý") và vần có dấu mũ, móc
  ("hoằ") chỉ có một cách đặt dấu nên giữ nguyên.

### 8. [names.py](../../app/services/retrieval/names.py): tra tên

**SQL `LOOKUP_SQL`** chạy trên cột `name_keys`, là mảng các dạng `norm()` của tên, tên tiếng Anh,
tên ASCII và các tên gọi khác:

- `exact` ("khớp tên") = có khoá bằng đúng query **hoặc bắt đầu bằng query + dấu cách**: phần
  chi nhánh "(…)" hay khẩu hiệu " - …" luôn đứng sau tên, nên "Katinat" khớp mọi chi nhánh còn
  "Chợ Lớn" không khớp "Cộng Cà Phê (152 Đường Chợ Lớn)". `exact_marked` = như vậy trên dạng
  giữ dấu (`name_keys_vi`), khi query có dấu.
- Độ giống = `similarity(k, q)` (đối xứng, theo trigram: chữ thừa bị trừ điểm), trên khoá giữ
  dấu khi query có dấu. Không dùng `word_similarity`: nó bỏ qua thứ tự từ ("Củ Chi" = "Chị Củ")
  và khớp tên đường trong địa chỉ chi nhánh.
- Giữ nơi khớp tên hoặc có độ giống ≥ 0.35; xếp khớp dấu, khớp tên, độ giống, tên ngắn hơn, rồi
  id; lấy 5.

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
"Ben Thanh", độ giống 0.71 và 0.56                            → resolved (0.71, cách 0.15)
"Ben",       hai nơi có tên bắt đầu bằng "Ben"                → ambiguous (cả hai)
"Chợ Lớn",   chỉ khớp trong địa chỉ một chi nhánh (0.30)       → not_found
```

Các ngưỡng là hằng số có tên ở đầu file (`RESOLVE_MIN`, `RESOLVE_GAP`, `AMBIGUOUS_MIN`,
`AMBIGUOUS_BAND`, `LOOKUP_LIMIT`), là giá trị khởi điểm, chỉnh bằng `eval/run_eval.py`.

**`exact_ids`** (chỉ dùng cho `avoid`): mọi nơi khớp tên (cùng điều kiện `exact`, nên gồm mọi
chi nhánh), không có LIMIT, rồi áp cùng quy tắc dấu qua `classify`. Điều kiện "bắt đầu bằng"
không dùng được GIN index nên quét mọi nơi, như `LOOKUP_SQL`.

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
    trong tên (`name_lex` với dạng thường, hoặc `word_similarity` ≥ 0.8 trên tên giữ dấu để chịu
    lỗi gõ) → bằng chứng `name`; chỉ khớp ở cuisine/features/mô tả (`search_lex` với **dạng
    chặt**, xem text.py) → bằng chứng `text`.
  - Một nơi khớp cả hai cách thì giữ bằng chứng mạnh hơn (`structured` được ghi trước).
- **`fetch_pois`**: lấy mọi nơi hợp lệ thuộc các node của kệ được yêu cầu, cộng các id khớp từ khoá
  và id `preferred`. Đọc đúng các cột trong `POI_COLUMNS`, đổi thành `Poi`.
- **`fetch_by_ids`**: lấy theo id **không** qua `ELIGIBLE`. Nơi người dùng đã chọn hoặc bắt buộc
  thì luôn hiện, kể cả khi bị đánh dấu không gợi ý.
- **`data_info`**: một query lấy danh sách cuisine riêng biệt và `data_version`, cache trong biến
  module 5 phút. Hai giá trị này chỉ đổi khi import lại.

### 9b. [semantic.py](../../app/services/retrieval/semantic.py): tìm theo nghĩa

`service._semantic` chạy sau `keyword_hits`, cho các yêu cầu tìm chữ (`InterestRequest.text`):

- **Bỏ qua** câu có phủ định (`negated`: "không", "chẳng", "đừng", "tránh", "khong", "chang",
  "ko", "no", "not", "without", "don't", "can't", "won't"...; trừ "không gian", "không khí",
  "không thể bỏ lỡ", "tránh nắng", "chạng vạng" (cả khi gõ "chang vang") và bia Chang), vì
  embedding của "không quá du lịch" nằm cạnh "du lịch". Chữ đi kèm group của "quán bar không ồn
  ào" cũng bị bỏ qua ở bước này; kệ bar vẫn trả lời ước muốn. Bỏ qua câu
  mà chữ đã khớp tên từ `SEMANTIC_NAME_HITS` nơi trở lên ("phở", "bánh mì"): embedding bỏ qua luật
  dấu.
- Chỉ `MAX_QUERIES` câu đầu được tìm theo nghĩa; các câu sau chỉ tìm theo chữ, kèm warning "Mỗi
  lần chỉ tìm theo ý nghĩa 5 sở thích tự do đầu tiên; “…” chỉ được tìm theo từ" (không báo
  "Không tìm thấy" cho chúng).
- Các câu chưa có trong `QUERY_CACHE` (LRU 512 câu mỗi process) được embed trong **một** lần gọi
  Gemini, dạng `task: search result | query: <câu>`. Không giữ kết nối DB trong lúc chờ. Gemini
  lỗi → warning "Tạm thời không tìm được theo ý nghĩa; sở thích tự do chỉ khớp theo từ".
- `SEMANTIC_SQL`: `FETCH_K` nơi hợp lệ gần nhất theo cosine (pgvector), giữ nơi có độ tương đồng
  ≥ `SIMILARITY_MIN`. Nơi đã có hit chữ giữ bằng chứng chữ; nơi mới có bằng chứng `semantic`
  (reason "Gợi ý theo mô tả"). `closeness` quy độ tương đồng về 0..1 giữa `SIMILARITY_MIN` và nơi
  gần nhất của chính câu đó, để xếp thứ tự.
- Mỗi nơi được embed lúc import dưới dạng `title: <tên> | text: <mô tả>`; `embedding_hash` đổi khi
  chữ, model hoặc số chiều đổi, và chỉ những dòng đó được embed lại.

| Hằng số | Giá trị | Ý nghĩa |
|---|---|---|
| `SIMILARITY_MIN` | 0.651 | Độ tương đồng tối thiểu của một hit theo nghĩa |
| `SEMANTIC_NAME_HITS` | 5 | Số nơi khớp tên làm tắt tìm theo nghĩa |
| `MAX_QUERIES` | 5 | Số câu tìm theo nghĩa mỗi request |
| `FETCH_K` | 120 (`SEMANTIC_KEEP` × 4) | Số nơi gần nhất lấy về trước các bộ lọc giờ, tránh, đã chọn |
| `QUERY_CACHE_SIZE` | 512 | Số câu giữ embedding trong mỗi process |

**Cách chọn `SIMILARITY_MIN`** (đo lại ngày 2026-11-24 trên `retrieval_test` sau khi viết lại và
embed lại mô tả: 437 dòng đổi so với trước, lần sửa cuối 25 dòng; lần đo đầu với `keyword_map.v1.1`):
`eval/run_eval.py --similarities` in 40 nơi gần nhất của mọi câu tự do trong 76 case eval (4
thành phố; 40 câu), cộng 25 câu thử. Trong 12 câu thử gốc chỉ 8 câu còn ghi lại được: 5 ước muốn
địa điểm `semantic-*`, "đi một mình", "cuối tuần", "đi cùng trẻ em"; cả 8 đều nằm trong tập đo.
Chạy lại eval với các ngưỡng 0.60-0.70 trên cùng vector (vector câu được cache; lần chạy trực tiếp
bằng `--similarities` cho đúng cùng thứ tự và độ tương đồng):

| Ngưỡng | Case qua (76) | Ghi chú |
|---|---|---|
| 0.60 | 67 | như 0.62, thêm mất "rooftop bar" (TP.HCM: Chạng Vạng không vào 5 nơi đầu) |
| 0.62-0.63 | 68 | "chỗ cho trẻ em" mất Đầm Sen khỏi 5 nơi đầu; "nhạc sống" (TP.HCM): 7 trong 8 nơi gần nhất là karaoke, công viên, quán cà phê |
| 0.64-0.645 | 69 | như 0.65 nhưng lẫn thêm nơi sai ("nhạc sống" ra karaoke, "sống ảo chụp ảnh" 2-6 nơi sai) |
| **0.65** | **69** | nơi thứ ba của "ngắm hoàng hôn" (Đà Nẵng, Marine Sky Bar) ở 0.652 |
| 0.655-0.66 | 67 | mất "ngắm hoàng hôn" (còn 2 rooftop bar), "trời mưa đi đâu trong nhà"; 0.66 được "uống rượu với bạn bè" (Hà Nội) nhưng mất cả "chỗ cho trẻ em" |
| 0.67-0.68 | 69 | được hai câu uống rượu ở Hà Nội, nhưng mất "ngắm hoàng hôn" và "chỗ cho trẻ em" (0.68) hoặc các bar Huế (0.67) |
| 0.70 | 65 | mất thêm "nhậu", "pub", "rooftop bar" (TP.HCM) và "lịch sử chiến tranh" |

Câu thử gốc ở 0.65: 5 ước muốn địa điểm có nơi gần nhất ở 0.684-0.759 và giữ 5-39 nơi đúng; "đi
một mình" (0.648) không có nơi nào trên ngưỡng; "đi cùng trẻ em" (0.716) vẫn có 20 nơi (gần nhất
là The Snap Cafe "hợp với gia đình có trẻ nhỏ", Estella, Đầm Sen); "cuối tuần" giờ là
`ignored_wishes`.

Giữ 0.65. Không ngưỡng nào chặn được hết câu không phải địa điểm: "đi cùng bạn bè" (0.712), "đi
cùng gia đình" (0.707), "lần đầu đến Sài Gòn" (0.723) gần hơn nơi tốt nhất của "yên tĩnh" (0.689)
hay "lịch sử chiến tranh" (0.684); chúng ra một section mang tên câu (King Beer 85, Karaoke Gia
Đình; nơi nổi tiếng). Từ chỉ thời gian thì đưa vào `ignored_wishes` ("cuối tuần" khớp nhà hàng
Wknd ở 0.742). Hai ngưỡng theo nguồn (`interest` / `other`) không giúp, vì cả ước muốn địa điểm
lẫn câu không phải địa điểm đều đến dưới dạng atom `other`. Lần đo này cũng cho thấy chữ đi kèm
group không phải chuyện ngưỡng: ngưỡng chỉ đổi việc hit rải rác của câu đó có "nhà" hay không, nên
chỗ sửa là cách đặt section (mục 12b), làm ngay sau đó.

**Đo lại sau khi sửa cách đặt section** (cùng ngày, `keyword_map.v1.2`, 92 case: 76 case trên
cộng 16 case cho các lỗi đã sửa, gồm "kiếm chỗ uống rượu" `eq` và "chỗ uống rượu" `prefer` ở Hà
Nội, Huế, Đà Nẵng, các case không embedder ở ba thành phố kia, "quán bar không ồn ào", "ăn hải
sản ven biển", "khám phá cố đô", "ngam chang vang tren cao"). Vector câu lấy lại từ các lần gọi
Gemini trước (cùng câu, cùng model), nên không tốn quota; kết quả ở
`eval/results/2026-11-24-after.txt` và `-after-nosemantic.txt`:

| Ngưỡng | Case qua (92) | Ghi chú |
|---|---|---|
| 0.60 | 85 | mất "rooftop bar" (TP.HCM), "chỗ cho trẻ em", "khám phá cố đô" (dưới 5 di tích, bảo tàng, chùa trong 8 nơi đầu) |
| 0.62-0.63 | 86 | như 0.60 nhưng giữ "rooftop bar" |
| **0.64-0.65** | **88** | |
| 0.655 | 86 | mất "trời mưa đi đâu trong nhà", "ngắm hoàng hôn" (Đà Nẵng) |
| 0.66 | 85 | như 0.655, mất thêm "chỗ cho trẻ em" |
| 0.67-0.68 | 85-84 | mất "ngắm hoàng hôn", "khám phá cố đô", "ngam chang vang tren cao" (0.68 cả "chỗ cho trẻ em") |
| 0.70 | 83 | qua "yên tĩnh" (Huế) nhưng mất thêm "craft beer", "lịch sử chiến tranh" |

Giữ 0.65 (mức cao nhất của dải tốt nhất). Không embedder (`--no-semantic`): 78/92; các case hỏng
thêm đều là case chỉ tìm theo nghĩa được ("ngắm hoàng hôn", "khám phá cố đô", "chỗ cho trẻ em"...)
hoặc chỉ đổi thứ tự trong kệ bar ("rooftop bar" và "craft beer" ở TP.HCM, rooftop bar ở Đà
Nẵng); mọi câu uống rượu vẫn ra đủ bar của thành phố, không có warning "Không tìm thấy" (R3).

**Đo lại sau khi sửa câu đồ uống trong mô tả bar** (cùng ngày): 49 mô tả bar bỏ câu chung "uống
rượu, bia hoặc cocktail" (nay là "uống rượu hoặc bia", và câu cocktail, rượu vang hay bia chỉ khi
chữ của chính dòng nói vậy), rồi `import_parquet.py --only description` và embed lại 49 dòng đó.
Ở 0.65: "cocktail" (TP.HCM) qua, nhưng Home Bar Là Nhà đi từ 0.643 lên 0.6504 với "trời mưa đi
đâu trong nhà" nên lọt vào section đó (case rain-hcm hỏng). Đo lại 0.65-0.653:

| Ngưỡng | Case qua (92) | Ghi chú |
|---|---|---|
| 0.65 | 88 | được "cocktail", mất "trời mưa đi đâu trong nhà" |
| **0.6505-0.652** | **89** | P@5 0.398 và nDCG@5 0.449 như 0.65 (0.652: 0.394 / 0.447) |
| 0.653 | 88 | mất nơi thứ ba của "ngắm hoàng hôn" (Đà Nẵng) |

Đổi thành 0.651 (cách Home Bar Là Nhà 0.0006, cách nơi thứ ba của "ngắm hoàng hôn" 0.001). Không
embedder: 79/92, chỉ thêm "cocktail". Ngoài eval, trong 54 câu đã cache vector chỉ có hai câu mất
nơi gần nhất của thành phố: "chỗ vui chơi cho trẻ em" ở Huế mất nơi duy nhất (Bảo tàng Thiên
nhiên Duyên hải miền Trung, 0.6504) và "chỗ cho trẻ em" ở Huế mất Spice Viet (một nhà hàng,
0.6507). Kết quả ở `eval/results/2026-11-24-after-i8.txt` và `-after-i8-nosemantic.txt`.

Các hằng số khác, ở 0.65 trên 92 case (giữ nguyên tất cả: 0.6, 0.3, 30, 5):

| Hằng số | Giá trị thử → case qua (hiện tại: 88) |
|---|---|
| `HOME_SHARE` 0.6 | 0.45, 0.5, 0.55 → 87 ("chỗ cho trẻ em" có nhà là đồ ăn, mất Đầm Sen); 0.65, 0.7 → 88, không đổi case nào |
| `WEAK_WEIGHT` 0.3 | 0.2, 0.5 → 88, không đổi case nào |
| `SEMANTIC_KEEP` 30 | 15, 20 → 87 (mất Đầm Sen của "chỗ cho trẻ em"); 40, 60 → 88, không đổi case nào |
| `SEMANTIC_NAME_HITS` 5 | 1 → 86 (mất "rooftop bar", "craft beer" ở TP.HCM); 3, 8 → 88, không đổi case nào |

Trước khi sửa cách đặt section, `HOME_SHARE` và `SEMANTIC_KEEP` còn đổi vài case uống rượu ở Hà
Nội, vì chúng quyết định chữ đi kèm có nhà hay không; nay chữ đi kèm luôn ở trong kệ của group nên
các case đó không còn phụ thuộc chúng. Bảng lần đo trước (76 case):

| Hằng số | Giá trị thử → case qua (lúc đó: 69) |
|---|---|
| `HOME_SHARE` 0.6 | 0.45-0.5 → 71: được "pub", "uống rượu với bạn bè" (Hà Nội), "rượu vang", nhưng "chỗ cho trẻ em" có nhà là đồ ăn nên mất Đầm Sen và các trung tâm thương mại; 0.52 → 69; 0.55 → 68; 0.65 → 68; 0.7 → 67 |
| `WEAK_WEIGHT` 0.3 | 0.2, 0.4, 0.5 → 69, không đổi case nào |
| `SEMANTIC_KEEP` 30 | 15 → 68; 20 → 69 (đổi "chỗ cho trẻ em" lấy "pub" Hà Nội); 40 → 69, không đổi case nào; 60 → 67 |
| `SEMANTIC_NAME_HITS` 5 | 1 → 68; 3 → 68 (mất "pub" TP.HCM); 8 và 100 → 69, không đổi case nào |

"chỗ cho trẻ em" không còn bar nào trong 8 nơi đầu (Estella, The Snap Cafe, Đầm Sen...), nên không
thêm luật loại node theo từ trong `keyword_map`. 4 case còn hỏng ở 0.65 không do ngưỡng (hỏng ở
mọi mức 0.60-0.68):

- "cocktail" (TP.HCM): 5 quán cocktail trong 8 nơi đầu của kệ bar, cần 6. Nơi 5-8 là bar thường
  có "cocktail" trong mô tả nhờ câu chung theo loại ("uống rượu, bia hoặc cocktail", 49 trong 92
  mô tả bar), nên khớp chữ ngang quán cocktail. Chỗ sửa là nội dung mô tả, không phải truy vấn;
  đã sửa sau đó (xem đoạn đo lại ở trên), nay case này qua.
- "nhậu" ở Hà Nội (cả check D2 của `check_backend.py`): trang đầu (6 nơi) của section "Nhậu" là 2
  quán nhậu Tự Do, Sparta Beer, Legend Beer, Bia hơi Cường Hói, Nút Quán; case cần 3 trong 5 quán
  tên "Bia hơi …". Mọi nơi đều khớp "nhậu" trong mô tả với cùng điểm, nên thứ tự giữa chúng do
  `explore` quyết định.
- "nhạc sống" (TP.HCM): nơi gần nhất 0.648 là phòng karaoke; mô tả các bar có nhạc sống chưa ghi
  chữ đó (Carmen Bar viết "có ban nhạc", Gang1968 Acoustic Beer không nhắc tới nhạc), nên chỉ 2 nơi
  khớp chữ.
- "yên tĩnh" (Huế): không bar nào trong 8 nơi đầu (R6 đạt), nhưng Tĩnh Garden Vegetarian (0.689)
  đứng đầu chỉ vì chữ "Tĩnh" trong tên; mô tả không nói yên tĩnh. Chỉ ngưỡng 0.70 bỏ được nó, và
  0.70 mất 6 case khác.

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

- Đếm hit của từ khoá theo category, có trọng số: hit `name`/`structured` tính 1, hit yếu (chỉ ở
  mô tả `text`, hoặc theo nghĩa `semantic`) tính `WEAK_WEIGHT` = 0.3.
- **Nhà** = category chiếm ≥ `HOME_SHARE` = 60% tổng trọng số; nếu không có thì thử theo top level;
  không có nữa thì không có nhà. Nhà tính trên mọi hit chữ cộng `SEMANTIC_KEEP` = 30 hit theo
  nghĩa gần nhất, **kể cả nơi đã chọn**, nên nhà không đổi khi người dùng chọn dần các nơi.
- Hit yếu **nằm ngoài nhà** bị bỏ. Ví dụ: một khu chợ có mô tả nhắc tới phở không phải là quán
  phở. Không có nhà thì hit yếu chỉ bị bỏ khi có ít nhất `STRONG_HITS_MIN` = 3 hit mạnh: một tên
  trùng ngẫu nhiên ("rượu" → nhà hàng Hầm Rượu) không được xoá cả chục hit mô tả.
- Sau khi tỉa mới giữ 30 hit theo nghĩa gần nhất **còn dùng được** (đang mở, không bị tránh, chưa
  chọn), nên chọn bớt nơi thì section được bù bằng nơi kế tiếp trong nhà.

**b. Dựng section** (`build_specs`):

- Mỗi sở thích dạng kệ → một section. Kệ không có node nào (`beach`) → warning "Không có dữ liệu
  cho “…” ở thành phố này".
- **Mặc định** (`may_use_defaults`, `uses_defaults`): khi không có sở thích (`source="interest"`)
  nào và không có group nào, chỉ có ước muốn `other` (hoặc không có gì), và sau khi tỉa không ước
  muốn nào **còn** nơi để hiện (`left`: nơi gợi ý được, đang mở, không bị tránh, chưa chọn) → dùng
  `defaults` (`food`, `culture`). Quyết định một lần, trên hit đã tỉa; `service.retrieve` luôn nạp
  nơi của kệ mặc định khi điều kiện đầu đúng. Ước muốn nào không **tìm ra** nơi nào (`found`: như
  `left` nhưng tính cả nơi đã chọn; không phủ định, không bị giới hạn 5 câu) có warning "Chưa tìm
  thấy địa điểm khớp “…”; đang gợi ý chung". Vì vậy ước muốn mà mọi nơi đã được chọn thì không có
  warning, còn ước muốn mà mọi nơi đều đóng cửa trong chuyến đi hoặc bị tránh thì có.
- Mỗi sở thích dạng từ khoá:
  - Không có hit → sở thích nêu trong `interests` có warning "Không tìm thấy địa điểm cho “…”",
    và nếu bắt buộc thì vẫn tạo section rỗng có `note`. Atom `other` không có hit thì không có
    section riêng (nó có thể không phải là địa điểm: "đi cùng trẻ em"); warning chỉ có khi trang
    đang hiện mặc định, như trên. Câu bị giới hạn 5 câu không bị báo là không tìm thấy.
  - Có kệ được yêu cầu bao nhà của nó → **gộp** vào kệ đó (`matched_keywords`), nơi khớp được đẩy
    lên đầu kệ.
  - Không có → section riêng, key `kw:<...>`.
  - **Chữ đi kèm group** (`companion`: "pub", "bia", "kiếm chỗ uống rượu" → `nightlife`) luôn
    được gộp vào kệ của group đó, dù nhà của nó ở đâu, nên các bar khớp chữ hay khớp nghĩa được
    đẩy lên đầu kệ bar và kệ không bao giờ mất nơi của mình. `companion_hits` giữ mọi hit nằm
    trong node của group; ngoài group thì nhà chỉ tính trên **hit chữ** (tên, mô tả), và chỉ khi
    có nhà thì các nơi ngoài group mới thành section riêng (Hà Nội "bia" → các quán "Bia hơi …",
    "nhậu" → các quán nhậu, cả hai là `food.restaurant`). Section đó (`outside`) không bao giờ
    nhận nơi của group. Quán cà phê, quán ăn chỉ gần nghĩa với "kiếm chỗ uống rượu" thì bị bỏ:
    kệ bar mới là câu trả lời. Chữ đi kèm không bao giờ có warning "Không tìm thấy".

**c. Đặt mỗi nơi vào đúng một section** (`_best_placement`):

- Bỏ nơi đã ghim (đã hiện ở `pinned`) và nơi bị luật tránh (`avoided`, gồm cả bộ lọc cột như "tránh
  đồ chay", bộ lọc này không làm được trong SQL `ELIGIBLE`).
- Kiểm tra giờ một lần; giờ đáng tin nói đóng → loại.
- Chấm điểm nơi đó ở **mọi** section nhận nó. Section cụ thể hơn thắng trước (node sâu hơn, rồi
  kệ ít node hơn; section từ khoá riêng là cụ thể nhất), rồi điểm cao hơn, rồi section bắt buộc,
  rồi thứ tự người dùng nêu. Vì vậy một quán cà phê nằm trong "Cà phê" chứ không nằm trong "Ẩm
  thực" khi người dùng yêu cầu cả hai, kể cả khi "Ẩm thực" là bắt buộc.
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

- **Chỉ 4 thành phố** (TP.HCM, Hà Nội, Đà Nẵng, Huế). Điểm đến khác trả `unsupported_destination`.
  Chưa có dữ liệu biển, spa, Cầu Rồng, Đại Nội: tìm các thứ đó chỉ ra warning hoặc nơi gần nghĩa.
- **Chữ đi kèm group chỉ tìm cả câu**: với câu dài chứa alias ("muốn đi nhậu với bạn bè"),
  chữ đi kèm là cả câu, nên tìm chữ không ra quán nhậu nào (không mô tả nào có cụm đó) và chỉ
  có kệ bar; "nhậu", "đi nhậu", "bia" thì vẫn có section riêng cho quán nhậu, bia hơi. Chữ đi kèm
  có phủ định sau alias ("quán bar không ồn ào") không được tìm theo nghĩa, nên kệ bar không xếp
  quán yên tĩnh lên trước.
- **Thứ tự giữa các nơi cùng bằng chứng**: mọi nơi chỉ khớp chữ trong mô tả có cùng điểm khớp,
  nên thứ tự giữa chúng do `explore` quyết định ("nhậu" ở Hà Nội: 1 trong 5 quán "Bia hơi …" ở
  trang đầu, cùng Sparta Beer, Legend Beer).
- **Câu chung theo loại trong mô tả bar**: bar mà chữ của chính dòng (tên, ghi chú, đoạn tra web)
  không nói loại đồ uống có câu chung "uống rượu hoặc bia" suy từ loại; "cocktail", "rượu vang",
  "bia" chỉ được viết khi dòng tự nói (Journie-Data `describe_vi.py`, `NIGHT_WORDS`).
- **25 dòng TP.HCM giữ đoạn nguồn tiếng Anh** (Wikivoyage, Wikipedia, kèm giấy phép) sau đoạn
  tiếng Việt. Không dịch hay bỏ, vì đó là đoạn có giấy phép và là bằng chứng đã dùng khi chọn
  ngưỡng (ví dụ "lịch sử chiến tranh").
- **Tên quán lọt vào embedding**: mỗi nơi được embed bằng tên + mô tả, nên một chữ trong tên có
  thể kéo nơi đó lên dù mô tả không nói gì: "yên tĩnh" ở Huế ra Tĩnh Garden Vegetarian (0.689)
  đứng đầu.
- **Câu không phải địa điểm vẫn ra section** khi nó gần một nhóm mô tả: "đi cùng gia đình" ra các
  quán hợp cho gia đình, "lần đầu đến Sài Gòn" ra các nơi nổi tiếng. Chỉ từ chỉ thời gian được bỏ
  (`ignored_wishes`).
- **`fetch_pois` nạp mọi nơi hợp lệ của các kệ vào Python** mỗi request và mỗi trang "xem thêm",
  vì giờ và điểm tính bằng Python. Ổn dưới khoảng 10k nơi hợp lệ; vượt ngưỡng thì cần xếp hạng sơ
  bộ trong SQL và LIMIT theo node (có comment `shortcut:` trong code).
- **Khi chỉ có ước muốn `other`** (ví dụ chỉ "quán yên tĩnh"), `service.py` vẫn nạp các nơi thuộc
  kệ mặc định (`food`, `culture`), vì chỉ biết có dùng mặc định hay không sau khi tỉa hit. Ước
  muốn ra nơi nào thì các nơi đó không được dùng; kết quả vẫn đúng, chỉ tốn công đọc thêm.
- **`LOOKUP_SQL` quét mọi dòng** (tính trigram trên từng tên), không dùng index. Ổn với dữ liệu
  hiện tại; dữ liệu lớn lên thì cần chuyển sang toán tử có index của `pg_trgm`.
- **Cache `data_info` theo từng process**: sau khi import, mỗi instance có thể thấy dữ liệu cũ tối
  đa 5 phút.
- **Lọc `section` làm sau khi đã tính toàn bộ**: "xem thêm" một section vẫn tốn công như request
  đầy đủ.
- **Các ngưỡng và trọng số**: `SIMILARITY_MIN` 0.651, `SEMANTIC_NAME_HITS` 5, `HOME_SHARE` 60%,
  `WEAK_WEIGHT` 0.3 và `SEMANTIC_KEEP` 30 được kiểm tra lại trên eval 76 case rồi 92 case ngày
  2026-11-24, sau khi embed lại, sau khi sửa cách đặt section và sau khi sửa câu đồ uống trong mô tả bar (xem 9b). 0.651 nằm sát hai phía: Home Bar Là Nhà ở 0.6504 với "trời mưa đi đâu trong nhà", nơi thứ ba của "ngắm hoàng
  hôn" ở 0.652, nên mỗi lần viết lại mô tả hoặc đổi model embedding thì phải đo lại. Các ngưỡng tên (0.6/0.15/0.35), trọng số điểm (0.55/0.15/0.15, quality 0 / explore 0.06),
  confidence 0.7 và giới hạn đa dạng vẫn là giá trị khởi điểm, chưa hiệu chỉnh trên dữ liệu lớn.
- **Cấu hình từ vựng** (`keyword_map`, `poi_overrides`) còn liệt kê tay. Xem thảo luận về việc
  chuyển chúng sang dữ liệu trước khi mở rộng.
