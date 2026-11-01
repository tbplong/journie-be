# CLAUDE.md

## Mục tiêu cốt lõi

Viết code mà người dùng **không phải viết lại**. Cụ thể:
- **Đúng với bài toán**, không chỉ đúng với ví dụ đã thấy.
- **Chịu được tăng trưởng** của dữ liệu và traffic một cách hợp lý.
- **Đơn giản và gọn**: diff nhỏ nhất có thể, không thừa abstraction.

Hai câu hỏi phải tự đặt trước khi viết code:
1. Giải pháp này còn đúng không nếu input **khác** ví dụ đã thấy?
2. Giải pháp này còn chạy được không nếu dữ liệu/traffic **tăng 10x, 100x**?

---

## 1. Đọc trước khi viết

Nguồn gốc lớn nhất của code tệ là viết trước khi hiểu codebase.

- **Đọc kỹ các file sắp sửa**, đọc thật chứ không lướt.
- **Làm theo pattern đã có** trong project: cấu trúc thư mục, cách đặt tên, cách xử lý lỗi, cách gọi DB/API.
- **Kiểm tra import và manifest** (package.json, requirements.txt, go.mod...) để biết project thực sự dùng gì. Project dùng `fetch` thì không mang `axios` vào.
- Tìm xem chức năng tương tự **đã tồn tại chưa** trước khi viết mới.
- Không tìm được pattern thì **hỏi**, không đoán.

---

## 2. Nghĩ trước khi code

- **Nêu rõ giả định.** "Thêm authentication" có thể là năm thứ khác nhau; nói rõ đã chọn cái nào.
- **Nêu trade-off** giữa các phương án khi có lựa chọn đáng kể.
- **Định nghĩa tiêu chí thành công trước khi viết code.** "Thêm validation" phải thành: "từ chối email thiếu hoặc sai format, trả về 400 với thông báo rõ ràng, có test cho cả hai trường hợp."
- **Xác định trục tăng trưởng**: cái gì sẽ tăng (users, records, requests, số loại đối tượng)?
- **Task nhiều bước: trình bày plan trước** để người dùng chặn hướng sai trước khi tốn một giờ build.
- Có điểm thực sự mơ hồ thì **dừng lại và hỏi**. Đừng lấp chỗ trống bằng code trông hợp lý; đó chính là loại code qua được review sơ sài rồi hỏng lúc quan trọng.

---

## 3. Tổng quát hoá: giải bài toán, không giải ví dụ

Phân biệt hai loại "hard-code":

**A. Hard-code theo ví dụ / test: KHÔNG BAO GIỜ được phép.**
- Không thêm nhánh `if input == "giá_trị_cụ_thể"` để pass test hoặc khớp ví dụ. Test fail thì sửa logic.
- Không liệt kê từng trường hợp khi có quy luật chung. Thấy mình viết chuỗi `if/elif` theo từng ID/tên cụ thể → dừng lại, tìm quy luật (mapping table, strategy, dữ liệu cấu hình).
- Không giả định hình dạng dữ liệu cố định (số phần tử, thứ tự, độ dài, format) trừ khi đó là contract rõ ràng. Phải xử lý: rỗng, một phần tử, rất nhiều phần tử, null/thiếu, trùng lặp.

**B. Hằng số cấu hình: được phép, nhưng phải có tên.**
- Một giá trị cấu hình (timeout, page size, ngưỡng) **có thể để là hằng số trong code** cho đến khi có lý do thật sự để đưa ra config. Không cần dựng hệ thống config cho một giá trị chưa bao giờ đổi.
- Nhưng **không có magic number trơ trọi**: `MAX_RETRY = 3  # giới hạn theo SLA của API X`.
- **Ngoại lệ bắt buộc đưa ra ngoài code**: secrets, credentials, URL/host khác nhau giữa các môi trường.

❌ Hard-code theo ví dụ:
```python
def get_discount(user):
    if user.id == 42 or user.id == 105:
        return 0.2
    return 0
```

✅ Theo quy luật:
```python
def get_discount(user, rules: DiscountRules):
    return rules.rate_for(user.tier)
```

❌ Giả định cố định:
```python
first, second, third = line.split(",")
```

✅ Theo contract:
```python
fields = line.split(",")
if len(fields) < REQUIRED_FIELDS:
    raise ParseError(f"Expected >= {REQUIRED_FIELDS} fields, got {len(fields)}")
```

---

## 4. Scalability

Đơn giản (mục 5) **không có nghĩa** là bỏ qua scalability. Chọn cấu trúc dữ liệu, thuật toán và cách truy cập I/O đúng thì gần như không tốn thêm độ phức tạp; đó là phần bắt buộc. Thứ cần tránh là **hạ tầng** chưa cần (queue, cache layer, microservice), không phải tư duy về quy mô.

### Ước lượng nhanh trước khi chốt thiết kế
- **Kích thước dữ liệu**: hiện tại? 1 năm nữa? trường hợp xấu nhất?
- **Độ phức tạp**: O(?) thời gian và bộ nhớ? Vòng lặp lồng nhau trên dữ liệu có thể lớn?
- **I/O**: bao nhiêu query/network call mỗi request? Có tăng theo số bản ghi không (N+1)?
- **Trạng thái**: có giữ gì trong memory process không? Chạy nhiều instance thì sao?

### Anti-pattern phải tránh
| Anti-pattern | Thay bằng |
|---|---|
| Load toàn bộ bảng/file vào memory | Pagination, streaming, cursor, batch |
| Query trong vòng lặp (N+1) | Batch query, JOIN, `IN (...)`, eager loading |
| O(n²) trên dữ liệu không bị giới hạn | Hash map/set, sort + scan, index |
| Collection, queue, cache, retry không giới hạn | Limit, TTL, max size, backoff |
| Session/state trong memory process | Store dùng chung (Redis, DB) khi cần nhiều instance |
| Tác vụ chậm chạy đồng bộ trong request | Background job khi thực sự cần |
| Thiếu index cho cột lọc/join | Thêm index, kiểm tra query plan |
| Gọi API ngoài không timeout | Timeout, retry có backoff |

### Không tự khoá đường mở rộng
- API trả về danh sách thì **có pagination từ đầu** (thêm sau là breaking change).
- Thêm một "loại" mới (payment method, định dạng file...) mà phải sửa nhiều file → cân nhắc registry hoặc cấu hình hướng dữ liệu, **khi đã có ít nhất 2–3 loại thực tế**.
- Cố ý chọn giải pháp không scale vì phạm vi nhỏ → **ghi rõ giới hạn**: `# Ổn với < 10k bản ghi; vượt ngưỡng cần chuyển sang batch.`

---

## 5. Đơn giản

- Viết **code tối thiểu giải quyết bài toán hiện tại**, không phải mọi phiên bản tương lai của nó.
- **Không abstraction sớm.** Phép thử: nếu lý do duy nhất để abstract là "phòng khi cần", thì đang over-build.
- **Copy-paste hai lần trước khi abstract.** Đến lần thứ ba mới đủ thông tin để thấy abstraction đúng.
- **Không xử lý lỗi cho trường hợp không thể xảy ra.** Nhưng lỗi có thể xảy ra (network, input người dùng, file thiếu, DB timeout) thì phải xử lý.
- Quyết định scalability tốn nhiều công sức hoặc độ phức tạp → **nêu trade-off và hỏi**, không tự quyết.

---

## 6. Thay đổi phẫu thuật (surgical changes)

- **Diff nhỏ nhất mà task cho phép.**
- **Không đụng vào thứ không được yêu cầu.** Không rename, không di chuyển file, không "dọn dẹp" ngoài phạm vi.
- **Theo style hiện có**, kể cả khi bạn thích style khác.
- **Không reformat.** Một lượt formatter chôn 3 dòng quan trọng giữa 300 dòng vô nghĩa.
- Phép thử: **mỗi dòng thay đổi phải giải thích được bằng task.** Dòng nào có mặt vì "tiện thì sửa luôn" → revert.
- Thấy vấn đề khác ngoài phạm vi → **ghi chú lại cho người dùng**, không tự sửa.

---

## 7. Kiểm chứng

Khoảng cách giữa code chạy được và code *nghĩ là* chạy được chính là testing.

- **Sửa bug: viết test fail trước, xem nó fail, rồi mới sửa.** Đó là bằng chứng duy nhất bạn sửa nguyên nhân chứ không phải triệu chứng.
- **Test hành vi có thể hỏng**, không test việc constructor gán field.
- **Test cả edge case từ mục 3** (rỗng, null, lớn, trùng lặp) và nhánh lỗi, không chỉ happy path.
- **Chạy test thật** trước khi báo xong; không tuyên bố "đã pass" khi chưa chạy.
- Thứ gì khó test là **thông tin về thiết kế**, không phải giấy phép bỏ qua.

---

## 8. Debug

Khi có lỗi: **điều tra, không đoán.**

1. **Đọc toàn bộ** thông báo lỗi và stack trace.
2. **Tái hiện lỗi** trước khi sửa bất cứ thứ gì.
3. **Thay đổi một thứ mỗi lần.**
4. Tìm nguyên nhân gốc. Không bịt một giá trị `null` bất ngờ bằng null check; tìm hiểu **tại sao nó null**, nếu không bug chỉ chuyển đến chỗ khó thấy hơn.
5. Sau 2–3 lần thử không thành, **dừng lại, tóm tắt những gì đã biết và hỏi**, thay vì thử ngẫu nhiên tiếp.

---

## 9. Dependency

Mỗi dependency là code vĩnh viễn bạn không kiểm soát.

- Trước khi thêm, hỏi: **project hoặc thư viện chuẩn đã làm được chưa?** (`crypto.randomUUID()` thay vì package `uuid`.)
- Khi thêm, **nói rõ lý do** để lựa chọn được nhìn thấy, không lén đưa vào manifest.
- Không thêm thư viện thứ hai cho việc project đã có thư viện làm (hai HTTP client, hai thư viện date...).

---

## 10. Giao tiếp

- **Nói đã làm gì và tại sao**, không chỉ đưa một khối code.
- **Nêu lo ngại** kể cả khi đã làm đúng yêu cầu.
- **Chính xác về sự không chắc chắn.** "Tôi không chắc thư viện này hỗ trợ streaming" cho người dùng biết cần kiểm tra gì; "Tôi nghĩ cái này sẽ chạy" thì không.
- Khi báo xong, nêu ngắn gọn: **giả định đã dùng**, **giả định quy mô**, **giới hạn đã biết**, và **điểm cần đổi nếu scale vượt ngưỡng**.

---

## 11. Các kiểu thất bại thường gặp

Bắt gặp mình rơi vào một trong các kiểu này thì **dừng lại**, không cố đẩy tiếp.

- **Example Fitting**: code khớp ví dụ/test thay vì giải bài toán (vi phạm mục 3).
- **Kitchen Sink**: tái cấu trúc nửa codebase trong lúc làm việc khác.
- **Wrong Abstraction**: abstract trước khi thấy đủ trường hợp thực tế.
- **Optimistic Path**: xử lý happy path, bỏ qua lỗi 500 và edge case.
- **Unbounded Growth**: code chạy tốt với dữ liệu test, sập khi dữ liệu thật lớn lên (load hết vào memory, N+1, O(n²), không limit).
- **Runaway Refactor**: một bản sửa lan sang hàng loạt file.
- **Guess-and-Check Debugging**: sửa thử liên tục mà không hiểu nguyên nhân.

---

## 12. Checklist tự review (bắt buộc trước khi báo hoàn thành)

**Phạm vi**
- [ ] Đã đọc các file liên quan và theo pattern có sẵn?
- [ ] Mỗi dòng thay đổi đều giải thích được bằng task? Không reformat, không sửa ngoài phạm vi?
- [ ] Tiêu chí thành công đã định nghĩa có được đáp ứng hết không?

**Tổng quát hoá**
- [ ] Có nhánh `if` nào chỉ tồn tại để khớp một test/ví dụ cụ thể?
- [ ] Code đúng với input khác ví dụ? Đã xử lý rỗng, null, cực lớn, trùng lặp?
- [ ] Magic number đã có tên? Secrets/URL môi trường đã ra khỏi code?

**Scalability**
- [ ] Độ phức tạp thời gian/bộ nhớ chấp nhận được khi dữ liệu tăng 100x?
- [ ] Không có query/network call trong vòng lặp?
- [ ] Mọi collection, cache, queue, retry đều có giới hạn? API ngoài có timeout?
- [ ] Không có state cản trở chạy nhiều instance (nếu hệ thống cần)?

**Đơn giản**
- [ ] Có abstraction nào chỉ tồn tại "phòng khi cần"?
- [ ] Có dependency mới nào? Đã giải thích lý do?

**Kiểm chứng**
- [ ] Đã chạy test thật? Bug fix có test tái hiện lỗi?
- [ ] Đã test nhánh lỗi và edge case, không chỉ happy path?