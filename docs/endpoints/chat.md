# `POST /chat`: giải thích code

## ELI5

Người dùng nói một câu lộn xộn: *"Đi Sài Gòn 2 ngày từ thứ Bảy, tối rảnh 6h–10h, thích cà phê,
nhất định phải ghé Dinh Độc Lập, đừng dắt đi bar"*. Máy không lập kế hoạch được từ một câu như vậy.
Nó cần một **phiếu yêu cầu** điền sẵn các ô: điểm đến, ngày, ngân sách, sở thích, nơi bắt buộc,
nơi cần tránh.

`/chat` là cô thư ký điền phiếu đó:

1. Cô đọc câu (nhờ Gemini, một mô hình ngôn ngữ).
2. Cô **kiểm tra phiếu** có đúng mẫu không (thiếu ô, sai kiểu thì trả lại).
3. Cô **sửa những lỗi hiển nhiên** mà không cần đoán (ví dụ ô "nơi cần tránh" có ghi nhưng danh
   sách ràng buộc quên ghi) và ghi lại đã sửa gì.
4. Cô **ghi chú** những chỗ còn nghi ngờ mà không tự sửa được.

Phiếu này (`extraction`) là đầu vào của [`/retrieval`](retrieval.md).

## Bản đồ file

| File | Vai trò | Có I/O? |
|---|---|---|
| [app/api/routes/chat.py](../../app/api/routes/chat.py) | Route HTTP: lấy Gemini client, gọi service, đổi exception thành lỗi HTTP | Không |
| [app/schemas/extraction.py](../../app/schemas/extraction.py) | Hình dạng request, response và **phiếu `Extraction`** (cũng là schema gửi cho Gemini) | Không |
| [app/services/extraction/service.py](../../app/services/extraction/service.py) | Điều phối pipeline: prompt → Gemini → validate → sửa → chẩn đoán | Qua client |
| [app/services/extraction/prompt.py](../../app/services/extraction/prompt.py) | System prompt `semantic-v3` (chép nguyên văn từ journie-data) | Không |
| [app/services/gemini.py](../../app/services/gemini.py) | HTTP client tối thiểu cho Gemini API | **Có** (HTTP) |
| [app/services/extraction/processing.py](../../app/services/extraction/processing.py) | `repair-v3` (sửa tất định) và `consistency-v2` (chẩn đoán chỉ đọc) | Không |
| [app/services/extraction/name_aliases.csv](../../app/services/extraction/name_aliases.csv) | Bảng tên gọi khác của điểm đến và sở thích → tên chuẩn | Đọc 1 lần lúc import |

Phần dùng chung (`main.py`, `config.py`, `errors.py`) xem [README.md](README.md).

## Flow một request

```
POST /chat {"text": "...", "reference_date": null}
│
├─ FastAPI validate body bằng ExtractionRequest ──── sai → 422
├─ Depends(get_gemini_client) ────────────────────── thiếu GEMINI_API_KEY → 503 llm-not-configured
│
└─ extract_constraints(client, text, reference_date)          service.py
   ├─ reference = reference_date hoặc "hôm nay" giờ Việt Nam
   ├─ build_system_prompt(reference)                          prompt.py
   ├─ client.generate_json(prompt, text, RESPONSE_SCHEMA)     gemini.py
   │    ├─ timeout ─────────────────────────────────────────── 504 llm-timeout
   │    ├─ HTTP 429 ────────────────────────────────────────── 503 llm-rate-limited (+Retry-After)
   │    ├─ lỗi mạng / HTTP khác / payload lạ ────────────────── 502 llm-upstream-error
   │    └─ Generation(text, finish_reason, usage...)
   ├─ text rỗng (bị chặn, hết token) ───────────────────────── 502 invalid-llm-output
   ├─ Extraction.model_validate_json(text) ─ sai schema ─────── 502 invalid-llm-output
   ├─ repair_extraction(parsed)  → (extraction, repairs)      processing.py
   ├─ consistency_issues(extraction) → issues                 processing.py
   └─ ExtractionResponse(extraction, repairs, issues, meta) ─── 200
```

Không có retry tự động ở bất kỳ bước nào. Lý do: cấu hình này đã được benchmark ở journie-data
không có retry, và một lần gọi Gemini đã mất cỡ vài giây đến vài chục giây. Client tự quyết có thử
lại hay không.

## Từng file

### 1. [routes/chat.py](../../app/api/routes/chat.py): route

- `get_gemini_client` là một **dependency**: FastAPI gọi nó trước hàm route. Nếu chưa cấu hình
  key, nó ném `ProblemError` 503 ngay, nên hàm `chat` không bao giờ chạy khi thiếu key. Nếu có,
  nó tạo `GeminiClient` bọc `http_client` dùng chung (tạo trong `lifespan`). Client rất nhẹ (chỉ
  giữ tham chiếu và header), nên tạo mới mỗi request không tốn gì.
- Hàm `chat` gọi `extract_constraints` và **dịch exception của tầng service sang lỗi HTTP**. Đây
  là chỗ duy nhất biết mã HTTP; tầng service chỉ ném exception mang ý nghĩa nghiệp vụ
  (`InvalidModelOutputError`, `GeminiTimeoutError`...).
- `responses={...}` với `problem_doc` chỉ để Swagger hiển thị các lỗi có thể xảy ra.

### 2. [schemas/extraction.py](../../app/schemas/extraction.py): hợp đồng dữ liệu

File này có hai nhóm model:

**Nhóm gửi cho Gemini: `Constraint`, `Extraction`**

- `StrictModel` đặt `extra="forbid"` (thừa field → lỗi) và `strict=True` (không tự ép kiểu:
  `"3"` không được chấp nhận cho `int`). Mục đích: output của mô hình phải **đúng y** hợp đồng,
  không được "gần đúng".
- `RESPONSE_SCHEMA = Extraction.model_json_schema()` được gửi cho Gemini làm
  `responseJsonSchema`. Gemini bị ràng buộc sinh JSON theo schema này. Vì thế file ghi rõ **không
  thêm description hay default** vào hai model này: làm vậy sẽ đổi schema gửi đi, tức là đổi
  request đã được benchmark.
- Hai cách biểu diễn cùng một sự thật trong `Extraction`:
  - **Field trực tiếp** (scalar và list): `destination`, `start_date`, `budget_vnd`, `interests`,
    `must_visit`...: dễ đọc, dễ dùng.
  - **Atom** (`hard_constraints` / `soft_constraints`, mỗi atom là `{field, operator, value}`):
    nói thêm *mức độ* (hard = bắt buộc, soft = ưu tiên) và *toán tử* (`eq`, `max`, `include`,
    `exclude`, `prefer`...). Ví dụ "tối đa 3 triệu" là `budget_vnd=3000000` **và** atom hard
    `budget_vnd max "3000000"`.
  - Hai cách này phải khớp nhau. Phần lớn công việc của `processing.py` là giữ chúng khớp.
- Field `other` trong atom: yêu cầu không có ô riêng (ví dụ "đi cùng trẻ em", "ăn phở"). Giữ
  nguyên chữ của người dùng.
- Validator `valid_calendar_date`: regex chỉ kiểm tra dạng `YYYY-MM-DD`, validator này còn kiểm
  tra ngày có thật (`2026-02-30` bị từ chối).

**Nhóm HTTP: `ExtractionRequest`, `ExtractionResponse`, `ConsistencyIssue`, `ExtractionMeta`,
`TokenUsage`**

- `ExtractionRequest.text`: 1–4000 ký tự, không được toàn khoảng trắng.
- `reference_date`: ngày dùng để hiểu "thứ Sáu tới", "cuối tuần này". Không gửi thì lấy hôm nay
  theo giờ Việt Nam. Gửi cố định thì kết quả tái lập được (hữu ích khi test).
- `ExtractionMeta` ghi lại model, các phiên bản prompt/sửa/chẩn đoán, độ trễ và số token, để biết
  một kết quả được sinh ra từ cấu hình nào.

### 3. [services/extraction/service.py](../../app/services/extraction/service.py): điều phối

`extract_constraints` là toàn bộ pipeline, đọc từ trên xuống:

1. Chọn `reference` (ngày tham chiếu), bắt đầu đếm giờ.
2. Gọi Gemini với system prompt (có ngày tham chiếu), câu của người dùng và schema.
3. `generation.text` rỗng → `InvalidModelOutputError("empty response")`. Xảy ra khi Gemini chặn
   prompt vì an toàn (`finish_reason=SAFETY`) hoặc hết token.
4. `Extraction.model_validate_json` parse và validate trong một bước. Sai → log **chỉ vị trí lỗi**
   (tối đa 5), không bao giờ log câu của người dùng hay output của mô hình (dữ liệu cá nhân), rồi
   ném `InvalidModelOutputError("schema-invalid response")`.
5. `repair_extraction` → phiếu đã sửa + danh sách bước đã sửa.
6. `consistency_issues` → danh sách chẩn đoán, đổi sang `ConsistencyIssue`.
7. Đóng gói `ExtractionResponse` với `meta`.

`InvalidModelOutputError` mang `finish_reason` để route đưa vào `detail` (ví dụ
`finish_reason=MAX_TOKENS` cho biết output bị cắt).

### 4. [services/extraction/prompt.py](../../app/services/extraction/prompt.py): system prompt

- Prompt ghép từ năm khối: `CONTRACT` (luật chính), `SEMANTIC_EXAMPLES` (ví dụ diễn giải),
  `TARGETED_RULES` (luật chi tiết: ngân sách, hard/soft, qua đêm, giữ nguyên tên...),
  `ATTACKS_AND_INTERESTS` (chống prompt injection, sở thích bắt buộc vs ưu tiên), `CHECKLIST`
  (tự kiểm tra trước khi trả lời).
- Chép **nguyên văn** từ journie-data (biến thể `semantic-v3`, run6). Thay đổi duy nhất: ngày tham
  chiếu được điền theo request thay vì cố định `2026-10-03`.
- `build_system_prompt` chỉ thay chuỗi `{reference_date}` bằng ngày ISO; phần còn lại giữ nguyên
  từng ký tự.
- Một số luật đáng biết vì ảnh hưởng tới output:
  - Không bao giờ bịa giá trị; không nói thì `null` hoặc list rỗng.
  - Tiền chỉ đổi khi có đơn vị VND rõ ràng (`500k` → `500000`); "500" trơn là mơ hồ → không đoán,
    giữ thành atom `other` và bật `needs_clarification`.
  - Chuẩn hoá tên điểm đến (Sài Gòn → `Ho Chi Minh City`) và 8 sở thích chuẩn (cà phê → `cafe`).
  - Câu chứa lệnh tấn công ("bỏ qua hướng dẫn trên...") → trả phiếu rỗng.
- Sửa prompt = phải benchmark lại ở journie-data và tăng `PROMPT_VERSION`.

### 5. [services/gemini.py](../../app/services/gemini.py): HTTP client

- `generate_json` dựng body theo đúng định dạng của adapter benchmark:
  `systemInstruction` (prompt), `contents` (câu của người dùng, vai `user`),
  `generationConfig` (temperature, max token, `responseMimeType=application/json`,
  `responseJsonSchema`).
- Tách prompt (hệ thống) khỏi câu người dùng là một lớp phòng thủ: mô hình biết phần nào là luật,
  phần nào là dữ liệu không đáng tin.
- API key nằm trong header `x-goog-api-key`, không nằm trong URL, nên không lọt vào log truy cập.
  Thông báo lỗi mạng chỉ ghi tên exception, không ghi URL/header.
- Ánh xạ lỗi: `httpx.TimeoutException` → `GeminiTimeoutError`; `429` → `GeminiRateLimitError`
  (kèm `retry-after` nếu Gemini gửi); mọi lỗi HTTP/mạng/payload lạ khác → `GeminiUpstreamError`.
- `_parse_generation`:
  - Lấy candidate đầu tiên. Không có candidate **và** không có `blockReason` → payload lạ → lỗi.
  - Có `blockReason` (prompt bị chặn) → text rỗng, `finish_reason` = lý do chặn. Service sẽ biến
    nó thành `invalid-llm-output`.
  - Ghép các `parts` có `text`, **bỏ phần `thought`** (suy nghĩ nội bộ của mô hình, nếu có).
  - Lấy số token từ `usageMetadata` cho `meta.usage`.

### 6. [services/extraction/processing.py](../../app/services/extraction/processing.py): sửa và chẩn đoán

Đây là file dài nhất và quan trọng nhất của `/chat`. Nguyên tắc ghi ở đầu file: **chỉ dùng giá trị
có sẵn trong output**, không đọc lại câu người dùng, không bịa giá trị, không bao giờ hạ
`needs_clarification` từ `true` xuống `false`. Nhờ vậy, mọi sửa đổi đều chứng minh được từ chính
phiếu.

**Các bảng quy tắc ở đầu file** (đọc chúng trước, phần còn lại chỉ là áp dụng):

| Bảng | Nghĩa |
|---|---|
| `MISSING_FIELD_RULES` | Khi nào một mục coi là thiếu. Ví dụ `travel_dates_or_duration` thiếu khi **cả ba** `start_date`, `end_date`, `duration_days` đều null (`all`); `available_time` thiếu khi **một trong hai** giờ null (`any`) |
| `LIST_ATOM_RULES` | List nào ứng với atom nào: `must_visit` ↔ hard `include`, `avoid` ↔ hard `exclude`, `interests` / `preferred_places` ↔ soft `prefer` |
| `LIST_ATOM_ALTERNATIVES` | `interests` còn được phép là hard `include` (sở thích bắt buộc) |
| `SCALAR_ATOM_OPERATORS` | Scalar nào ứng với atom nào: hầu hết `eq`, riêng `budget_vnd` nhận cả `max` |

**`repair_extraction`: các bước theo thứ tự**, mỗi lần sửa ghi một tên vào `repairs`:

1. **`canonical_name`**: đổi tên gọi khác sang tên chuẩn cho `destination`, `interests` và các
   atom tương ứng, dùng `name_aliases.csv`. Khoá so khớp `place_key`: NFC, bỏ phân biệt hoa
   thường, bỏ khoảng trắng/chấm/gạch. Dấu **được giữ**, nên các cách viết không dấu phải liệt kê
   trong CSV. Trùng lặp trong `interests` bị gộp.
2. **`dedupe_constraint`**: xoá atom trùng y hệt **trong cùng một list**. Một cặp trùng giữa hard
   và soft thì không xoá, vì không biết bên nào đúng.
3. **Đồng bộ list ↔ atom**:
   - `list_from_constraint`: atom có giá trị mà list chưa có → thêm vào list.
   - `constraint_from_list`: list có giá trị mà chưa có atom → thêm atom (chỉ cho field có **một**
     quy tắc duy nhất). `interests` bị bỏ qua vì không biết sở thích đó là hard hay soft. Đây là
     lý do bạn thường thấy issue `missing_atom` cho `interests`.
4. **`scalar_from_constraint`**: scalar null/khác nhưng atom hard nói rõ đúng **một** giá trị →
   điền scalar. Với ngân sách, atom `max` được ưu tiên hơn `eq`. Giá trị phải là số nguyên chuẩn
   (`"3000000"`, không phải `"3tr"`) và phải qua validate, nếu không thì bỏ qua.
5. **`conflict_from_observation`**: tính các mâu thuẫn **kiểm chứng được** từ giá trị:
   - `date_order`: ngày kết thúc trước ngày bắt đầu.
   - `duration_disagreement`: `duration_days` khác số ngày tính từ hai mốc (tính cả hai đầu).
   - `budget_disagreement`: có `min` lớn hơn `max`.
   - `must_visit_avoid`: cùng một nơi vừa bắt buộc vừa tránh.

   Quan sát được → thêm vào `possible_conflicts`; chứng minh được là không có → xoá khỏi đó;
   không đủ dữ liệu (`None`) → để nguyên.
6. **`clarification_from_conflict`**: còn mâu thuẫn mà `needs_clarification=false` → bật lên
   `true`. Chỉ bật, không bao giờ tắt.
7. **`missing_fields`**: tính lại từ các ô null theo `MISSING_FIELD_RULES`.

Mỗi bước dựng lại `Extraction` bằng `model_validate`, nên một bước sửa không thể tạo ra phiếu sai
schema.

**`consistency_issues`: chẩn đoán chỉ đọc.** Không sửa gì, chỉ báo. Mỗi issue có `severity`:
`error` (sai biểu diễn chắc chắn) hoặc `review` (cần người xem). Các mã:

| Mã | Khi nào |
|---|---|
| `duplicate_atom` | Atom trùng giữa hard và soft |
| `atom_classification_mismatch` | Atom dùng toán tử không hợp lệ cho field/list đó (ví dụ hard `prefer`) |
| `missing_atom` | Scalar hoặc list có giá trị nhưng không có atom tương ứng |
| `scalar_atom_mismatch` | Scalar khác giá trị trong atom |
| `missing_scalar` | Atom nói rõ một giá trị nhưng scalar để null |
| `list_atom_mismatch` | Atom có giá trị mà list không có |
| `missing_fields_mismatch` | `missing_fields` không khớp các ô null |
| `budget_bound_requires_review` | Giới hạn ngân sách không phải số nguyên chuẩn |
| `conflict_metadata_mismatch` / `_unverifiable` | Mâu thuẫn khai báo khác với quan sát / không kiểm chứng được |
| `overnight_relation_requires_review` | Giờ kết thúc trước giờ bắt đầu (có thể qua đêm, schema không biểu diễn được) |
| `conflict_requires_clarification` | Có mâu thuẫn mà `needs_clarification=false` |

Vì sao tách "sửa" và "chẩn đoán": chỉ những gì có **đúng một cách sửa** mới được sửa tự động;
phần còn lại phải để lộ ra cho client hoặc người xem quyết định, không được giấu.

### 7. [name_aliases.csv](../../app/services/extraction/name_aliases.csv): bảng tên gọi khác

Ba cột `kind,canonical,alias`. Hiện có 65 dòng cho điểm đến và 24 dòng cho sở thích. Nạp **một
lần** khi import module (`NAME_ALIASES`). Nếu một alias trỏ tới hai tên chuẩn khác nhau, app từ
chối khởi động. `/retrieval` cũng dùng bảng này để nhận ra sở thích chuẩn.

## Ví dụ chạy thật của bước sửa

Phiếu đầu vào (giả sử Gemini trả như vậy):
`interests=["cafe","phở","michelin"]`, `must_visit=["Dinh Độc Lập"]`, `avoid=["bar"]`, không có
atom nào.

Chạy `repair_extraction` và `consistency_issues` (output thật):

```
repairs = ['constraint_from_list', 'constraint_from_list', 'missing_fields']
hard_constraints = [
  {'field': 'must_visit', 'operator': 'include', 'value': 'Dinh Độc Lập'},
  {'field': 'avoid',      'operator': 'exclude', 'value': 'bar'},
]
missing_fields = ['budget']
issues: missing_atom cho destination, start_date, duration_days, available_start,
        available_end (scalar có giá trị nhưng không có atom), và interests
```

Đọc kết quả:

- `must_visit` và `avoid` có đúng một quy tắc nên được thêm atom tự động.
- `interests` không được thêm atom (không biết hard hay soft), nên thành issue.
- Các scalar có giá trị mà thiếu atom cũng chỉ được báo, không được sửa: thêm atom là bịa ra một
  khẳng định "hard" mà mô hình chưa nói.

## Cơ chế đằng sau: vì sao thiết kế như vậy

- **Ba lớp phòng thủ cho output LLM.** (1) Gemini bị ràng buộc schema khi sinh; (2) Pydantic
  strict kiểm tra lại, vì ràng buộc phía Gemini không đảm bảo tuyệt đối; (3) bước sửa tất định
  xử lý những lỗi nhất quán mà schema không bắt được (list và atom lệch nhau).
- **Mọi thứ có phiên bản.** `PROMPT_VERSION`, `PROCESSING_VERSION`, `DIAGNOSTICS_VERSION` đi vào
  `meta`. Đổi bất kỳ phần nào phải đo lại bằng benchmark ở journie-data (độ chính xác hiện tại
  83.6% case khớp hoàn toàn trên dataset v3), rồi tăng version.
- **Không giữ trạng thái.** Mỗi request độc lập, không có session, không có cache. Chạy nhiều
  instance (Vercel serverless) không cần phối hợp gì.
- **Chi phí theo request.** Mỗi request = đúng một lời gọi Gemini có tính phí. Không có đăng nhập
  hay rate limit trong code; README khuyên đặt rule rate limit ở Vercel Firewall.

## Giới hạn đã biết

- Một lần gọi có thể mất vài chục giây (ví dụ thật ~33 giây). Client nên đặt timeout ≥ 60 giây,
  khớp `GEMINI_TIMEOUT_SECONDS`.
- Không retry: lỗi tạm thời của Gemini trả thẳng về client.
- Bảng alias chỉ có 8 điểm đến chính; điểm đến khác giữ nguyên chữ mô hình trả về.
- Chất lượng trích xuất phụ thuộc prompt và mô hình; repo này chỉ chứa bản đã benchmark, không chứa
  công cụ đo.
