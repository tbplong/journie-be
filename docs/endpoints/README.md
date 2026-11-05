# Giải thích code theo endpoint

Mỗi endpoint có một file riêng, giải thích từng file code nó chạy qua, theo thứ tự request đi qua:

| Endpoint | File | Nội dung |
|---|---|---|
| `POST /chat` | [chat.md](chat.md) | Câu tự do → Gemini → kiểm tra → sửa tất định → `extraction` |
| `POST /retrieval` | [retrieval.md](retrieval.md) | `extraction` → tra tên, lọc, chấm điểm, chia section (không LLM) |
| `GET /health` | [health.md](health.md) | Kiểm tra server còn sống |
| `GET /` | [root.md](root.md) | Endpoint mẫu |

Tài liệu cho người **gọi** API (field, lỗi, ví dụ curl) nằm ở [../API.md](../API.md). Các file ở
đây dành cho người **đọc và sửa code**.

## Phần dùng chung cho mọi endpoint

Mọi request đều đi qua bốn file này trước khi tới code riêng của endpoint.

### [app/main.py](../../app/main.py): khởi động app

- Tạo `FastAPI(title="Journie API", lifespan=lifespan)`, đăng ký error handler, gắn router.
- `lifespan` chạy **một lần khi process khởi động** và dọn dẹp khi process tắt. Nó tạo hai tài
  nguyên dùng chung, cất vào `app.state`:
  - `app.state.http_client`: một `httpx.AsyncClient` duy nhất cho mọi lời gọi Gemini. Dùng chung
    để tái sử dụng kết nối TCP/TLS thay vì mở mới mỗi request. `follow_redirects=False` để API key
    (nằm trong header) không bao giờ bị gửi sang host khác khi bị redirect.
  - `app.state.db_pool`: pool kết nối Postgres (xem `db.py` trong [retrieval.md](retrieval.md)),
    hoặc `None` nếu không có `DATABASE_URL`. Pool mở kiểu `wait=False` với `min_size=0`: khởi
    động không chờ database, kết nối đầu tiên chỉ được mở khi có request `/retrieval`.
- Vì sao cần lifespan thay vì tạo client trong từng route: tạo client/pool là việc tốn kém, còn
  `app.state` là chỗ FastAPI cho phép giữ tài nguyên sống suốt vòng đời process.

### [app/api/router.py](../../app/api/router.py): gom router

Chỉ `include_router` bốn router con (`root`, `health`, `chat`, `retrieval`). Thêm endpoint mới thì
thêm một dòng ở đây.

### [app/core/config.py](../../app/core/config.py): cấu hình

- `Settings` (pydantic-settings) đọc biến môi trường hoặc file `.env`. Tên field = tên biến môi
  trường không phân biệt hoa thường (`gemini_api_key` ↔ `GEMINI_API_KEY`).
- Secrets (`gemini_api_key`, `database_url`) có kiểu `SecretStr`: in ra log chỉ thấy `**********`,
  muốn lấy giá trị thật phải gọi `.get_secret_value()`.
- Các ràng buộc như `Field(ge=1)` làm app **từ chối khởi động** nếu cấu hình sai, thay vì lỗi giữa
  chừng một request.
- `get_settings()` có `@lru_cache`: chỉ đọc môi trường một lần mỗi process. Route dùng nó qua
  `Depends(get_settings)`, nên test có thể thay bằng `app.dependency_overrides`.

### [app/core/errors.py](../../app/core/errors.py): định dạng lỗi

- `ProblemError(status, slug, title, detail, headers)` là exception duy nhất code app ném ra cho
  lỗi có chủ đích. `problem_handler` biến nó thành JSON theo RFC 9457
  (`application/problem+json`):
  ```json
  {"type": "/problems/llm-timeout", "title": "LLM timeout", "status": 504,
   "detail": "...", "instance": "/chat"}
  ```
  `type` là mã ổn định để client `switch` theo; `detail` là chữ cho người đọc, có thể đổi.
- `problem_doc(description, instance)` chỉ để khai báo các response lỗi trong OpenAPI (`/docs`),
  không ảnh hưởng runtime.
- Lỗi validate body (422) không đi qua đây: FastAPI tự trả theo định dạng mặc định của nó.

### Mô hình chung: route mỏng, service dày

Cả hai endpoint chính theo cùng một khuôn:

```
app/api/routes/<x>.py      HTTP: nhận body, lấy dependency, đổi exception → ProblemError
app/schemas/<x>.py         Pydantic: hình dạng request/response, validate ở biên
app/services/<x>/...       Logic nghiệp vụ, không biết gì về HTTP
```

Nhờ vậy logic trong `services/` test được mà không cần dựng HTTP server, và route chỉ còn vài
dòng.
