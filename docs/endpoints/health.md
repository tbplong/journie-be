# `GET /health`

**Công dụng:** cho load balancer, uptime monitor hoặc người vận hành biết process còn nhận request.

**File:** [app/api/routes/health.py](../../app/api/routes/health.py), 8 dòng.

```python
@router.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
```

**Flow:** request → router → hàm trả về `{"status": "ok"}` với 200. Không đọc config, không gọi
Gemini, không chạm database.

**Cơ chế cần biết:**

- Hàm là `def` thường, không phải `async def`. FastAPI chạy hàm `def` trong threadpool; với một
  hàm trả hằng số thì không khác biệt gì.
- `tags=["health"]` chỉ để nhóm endpoint trong Swagger (`/docs`).

**Giới hạn:** đây là kiểm tra "process sống", không phải "hệ thống sẵn sàng". `/health` vẫn trả
`ok` khi thiếu `GEMINI_API_KEY` hoặc database không kết nối được. Nếu cần kiểm tra sâu hơn (ví dụ
`SELECT 1` vào Postgres) thì phải thêm endpoint khác, vì làm `/health` phụ thuộc database sẽ
khiến monitor báo chết cả app khi chỉ database có vấn đề.
