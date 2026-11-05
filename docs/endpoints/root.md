# `GET /`

**Công dụng:** endpoint mẫu từ lúc khởi tạo project. Trả `{"Hello": "World"}`. Không có client
nào phụ thuộc vào nó; tiện để thử nhanh server đã chạy chưa.

**File:** [app/api/routes/root.py](../../app/api/routes/root.py), 8 dòng.

```python
router = APIRouter()

@router.get("/")
def root() -> dict[str, str]:
    return {"Hello": "World"}
```

**Flow:** request → router → trả hằng số, 200. Không phụ thuộc gì.

**Ghi chú:** router này không có `tags`, nên trong Swagger nó nằm ở nhóm "default". Muốn kiểm tra
sức khoẻ server thì dùng [`/health`](health.md), không dùng `/`.
