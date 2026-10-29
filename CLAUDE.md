# CLAUDE.md

## Mục tiêu cốt lõi

Viết code **tổng quát hoá đúng mức** và **chịu được tăng trưởng**. Giải pháp chỉ chạy được với dữ liệu mẫu, ví dụ trong đề bài hoặc quy mô hiện tại thì chưa xong.

Trước khi viết code, luôn tự hỏi:
1. Giải pháp này còn đúng không nếu input **khác** ví dụ đã thấy?
2. Giải pháp này còn chạy được không nếu dữ liệu/traffic **tăng 10x, 100x**?

---

## 1. Không hard-code: giải quyết bài toán, không giải quyết ví dụ

### Quy tắc
- **Không viết code chỉ để pass test hoặc khớp ví dụ.** Nếu test fail, sửa logic, không thêm nhánh `if input == "giá_trị_cụ_thể"`.
- **Không nhúng giá trị nghiệp vụ vào logic.** ID, tên, URL, ngưỡng (threshold), giới hạn, số lượng, đường dẫn, credentials, danh sách lựa chọn → đặt vào config, env var, hằng số có tên, hoặc lấy từ dữ liệu.
- **Suy ra quy luật, không liệt kê trường hợp.** Khi thấy mình viết chuỗi `if/elif` cho từng case cụ thể, dừng lại và tìm quy luật chung (mapping table, strategy, rule engine, dữ liệu cấu hình).
- **Không giả định hình dạng dữ liệu cố định** (số phần tử, thứ tự, độ dài, format) trừ khi đó là contract rõ ràng. Phải xử lý: rỗng, một phần tử, rất nhiều phần tử, giá trị null/thiếu, trùng lặp.
- **Magic number phải có tên và lý do.** `MAX_RETRY = 3  # giới hạn theo SLA của API X` thay vì `3` trơ trọi.

### Ví dụ

❌ Hard-code theo ví dụ:
```python
def get_discount(user):
    if user.id == 42 or user.id == 105:
        return 0.2
    return 0
```

✅ Tổng quát hoá:
```python
def get_discount(user, rules: DiscountRules):
    return rules.rate_for(user.tier)
```

❌ Giả định cố định:
```python
first, second, third = line.split(",")
```

✅ Xử lý theo contract:
```python
fields = line.split(",")
if len(fields) < REQUIRED_FIELDS:
    raise ParseError(f"Expected >= {REQUIRED_FIELDS} fields, got {len(fields)}")
```

### Khi nào hard-code là chấp nhận được
- Hằng số vật lý/toán học, giá trị thực sự không bao giờ đổi.
- Script dùng một lần mà người dùng **nói rõ** là throwaway.
- Ngay cả khi đó, vẫn đặt tên hằng số.

---

## 2. Luôn suy nghĩ về scalability

### Trước khi chốt một thiết kế, ước lượng nhanh
- **Kích thước dữ liệu**: bây giờ bao nhiêu? 1 năm nữa bao nhiêu? Trường hợp xấu nhất?
- **Độ phức tạp**: thuật toán là O(?) theo thời gian và bộ nhớ? Có vòng lặp lồng nhau trên dữ liệu có thể lớn không?
- **I/O**: bao nhiêu query DB / network call trên mỗi request? Có tăng theo số bản ghi không (N+1)?
- **Trạng thái**: có lưu gì trong memory của process không? Chạy nhiều instance thì sao?

Ghi ngắn gọn ước lượng này trong phần giải thích hoặc comment khi thiết kế có ảnh hưởng đến hiệu năng.

### Các anti-pattern phải tránh
| Anti-pattern | Thay bằng |
|---|---|
| Load toàn bộ bảng/file vào memory | Pagination, streaming, cursor, batch |
| Query trong vòng lặp (N+1) | Batch query, JOIN, `IN (...)`, eager loading |
| O(n²) trên dữ liệu không bị giới hạn | Hash map/set, sort + scan, index |
| Không có giới hạn (unbounded) cho list, queue, cache, retry | Luôn có limit, TTL, max size, backoff |
| Lưu session/state trong memory process | Store dùng chung (Redis, DB) nếu cần chạy nhiều instance |
| Xử lý đồng bộ tác vụ chậm trong request | Queue / background job khi phù hợp |
| Thiếu index cho cột dùng để lọc/join | Thêm index, kiểm tra query plan |
| Gọi API ngoài không timeout | Timeout, retry có backoff, circuit breaker khi cần |

### Thiết kế để mở rộng
- **Thêm một loại mới không nên phải sửa nhiều chỗ.** Nếu thêm một payment method / loại user / định dạng file mà phải sửa 5 file, hãy cân nhắc registry, interface, hoặc cấu hình hướng dữ liệu.
- **Tách biệt cái thay đổi và cái ổn định.** Logic nghiệp vụ hay thay đổi → tách khỏi phần hạ tầng.
- **API và schema nghĩ cho tương lai**: pagination từ đầu, field có thể mở rộng, versioning khi public.

---

## 3. Cân bằng: không over-engineer

Tổng quát hoá và scalability phải **tương xứng với bối cảnh**. Đừng thêm microservices, abstraction nhiều tầng, hay plugin system cho một tính năng nhỏ.

- Ưu tiên giải pháp **đơn giản nhưng không tự khoá đường mở rộng**.
- Nguyên tắc: dễ đổi sau này > tối ưu sớm. Chọn cấu trúc cho phép scale khi cần, chưa cần thì chưa build hạ tầng.
- Nếu một quyết định scalability tốn đáng kể công sức hoặc độ phức tạp, **nêu rõ trade-off và hỏi** thay vì tự quyết.
- Nếu cố ý chọn giải pháp không scale (vì phạm vi nhỏ), **ghi rõ giới hạn**: `# Ổn với < 10k bản ghi; vượt ngưỡng này cần chuyển sang batch.`

---

## 4. Quy trình làm việc

1. **Hiểu bài toán tổng quát** trước khi nhìn ví dụ. Viết lại yêu cầu bằng lời của mình, dưới dạng quy luật chứ không phải danh sách case.
2. **Xác định trục tăng trưởng**: cái gì sẽ tăng (users, records, requests, số loại)?
3. **Thiết kế** sao cho logic không phụ thuộc giá trị cụ thể và chi phí tăng hợp lý theo trục đó.
4. **Implement.**
5. **Tự review theo checklist bên dưới** trước khi báo xong.

## 5. Checklist tự review (bắt buộc trước khi báo hoàn thành)

- [ ] Có giá trị cụ thể nào (ID, tên, URL, số) bị nhúng trong logic không? Đã chuyển ra config/hằng số chưa?
- [ ] Code có còn đúng với input khác ví dụ không? Đã nghĩ tới rỗng, null, cực lớn, trùng lặp chưa?
- [ ] Có nhánh `if` nào chỉ tồn tại để pass một test/case cụ thể không?
- [ ] Độ phức tạp thời gian/bộ nhớ là gì? Có chấp nhận được khi dữ liệu tăng 100x không?
- [ ] Có query/network call trong vòng lặp không?
- [ ] Mọi collection, cache, queue, retry đều có giới hạn chưa?
- [ ] Có state nào khiến không chạy được nhiều instance không?
- [ ] Thêm một "loại" mới thì phải sửa bao nhiêu chỗ?
- [ ] Mình có đang over-engineer so với phạm vi thực tế không?

Khi báo cáo kết quả, nêu ngắn gọn: **giả định quy mô**, **giới hạn đã biết**, và **điểm cần thay đổi nếu scale vượt ngưỡng**.