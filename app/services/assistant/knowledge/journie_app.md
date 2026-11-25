# Journie hoạt động thế nào

Tóm tắt từ mã nguồn frontend (journie-fe-main/src). Tên nút giữ đúng chữ trên màn hình; tên tiếng Anh ở trong ngoặc.

## Bốn thành phố có dữ liệu | Cities Journie covers

Journie hiện có địa điểm ở bốn thành phố: TP.HCM (Sài Gòn), Hà Nội, Đà Nẵng và Huế. Mỗi lịch trình chỉ gợi ý địa điểm của đúng thành phố đã chọn. Thành phố khác (Đà Lạt, Hội An, Nha Trang...) chưa có dữ liệu, nên Journie không gợi ý được nơi nào ở đó. Dữ liệu mỗi thành phố có số loại nơi khác nhau: TP.HCM nhiều nhất; Đà Nẵng và Huế ít hơn nhiều. Loại nơi nào không có trong dữ liệu thì Journie không gợi ý được, ví dụ hiện chưa có bãi biển nào trong dữ liệu Đà Nẵng.

## Tạo một chuyến đi | Creating a trip

Vào "Lên một chuyến đi" (Plan a trip), chọn điểm đến, số ngày, ngày khởi hành và ngân sách cả chuyến (không bắt buộc). Có ba cách:
- "Tạo bằng AI" (Generate with AI): viết vài dòng điều bạn thích và điều muốn tránh, ví dụ "mê phở, ngại chen chúc". Journie đọc câu đó, hỏi "Đúng ý bạn chưa?"; bấm "Sửa lại" (Edit) nếu chưa đúng, rồi "Xem nơi hợp với yêu cầu" để chọn nơi. Những nơi bạn tích chắc chắn có trong lịch trình.
- "Bắt đầu trống" (Start blank): tự thêm từng nơi vào từng ngày.
- "Nhân bản chuyến cũ" (Copy a trip): giữ nguyên các điểm theo ngày, chỉ đổi ngày đi. Bản sao là bản nháp, kiểm tra rồi lưu.
Cũng có thể "Dùng bộ sưu tập đã lưu": các nơi trong bộ sẽ là điểm bắt buộc.

## Nhịp độ và cách di chuyển | Pace and getting around

Nhịp độ có ba mức: "Chậm" (Slow), "Vừa" (Balanced), "Nhanh" (Fast). Bộ xếp lịch giới hạn số điểm mỗi ngày theo nhịp độ: hiện là 4, 6 và 8 điểm (tính cả bữa ăn và cà phê), trừ khi bạn tự đặt "điểm mỗi ngày". Cách di chuyển: "Đi bộ" (Walk), "Xe máy" (Bike) hoặc Taxi; thời gian đi giữa các điểm được tính theo cách bạn chọn. Ngày mặc định chạy từ 08:00 đến 21:00; chuyến có đi chơi đêm thì ngày bắt đầu và kết thúc muộn hơn. Giới hạn chính xác mỗi ngày (số quán cà phê, bữa ăn, quán bar) do bộ xếp lịch của Journie quyết định.

## Xếp giờ, khoá điểm và lưu | Scheduling, locking and saving

Bấm "Tạo lịch trình" (Create schedule) để Journie xếp giờ cho các nơi đã chọn: kiểm tra giờ mở cửa của ngày đó, thời gian đi lại và ngân sách. Lịch trình mở ra dạng bản nháp; xem lại rồi bấm "Lưu lịch trình" (Save itinerary), hoặc bỏ nếu chưa ưng. Trong lịch trình có thể chuyển một điểm lên hoặc xuống, thêm hoặc bớt 15 phút, xoá một điểm. "Khóa điểm này" (Lock this stop) nghĩa là điểm đó bắt buộc phải giữ; khi bấm "Gợi ý lại" (Suggest again), nơi đã khoá giữ nguyên ngày. Nếu có nơi không xếp vừa, hãy xoá bớt một nơi hoặc bỏ khoá rồi bấm "Tạo lịch trình" lại. Quán bar không được xếp trước 18:00.

## Chế độ đang đi và đổi kế hoạch | Live mode and replanning

"Mở chế độ đang đi" (Open live mode) theo dõi chuyến đi trong ngày: Journie kiểm tra giao thông, thời tiết và giờ mở cửa mỗi phút. Sự cố gồm mưa lớn, kẹt xe, điểm đóng cửa hoặc trễ giờ. Khi có sự cố, Journie đưa ra "Phương án điều chỉnh": "Giữ gần như nguyên" (Keep it close), "Cân bằng" (Balanced), "Trải nghiệm tốt nhất" (Best experience) hoặc "Theo ý bạn" (Your mix); bạn cũng có thể giữ lịch hiện tại. Khi mưa lớn, phương án mới không bắt đầu điểm ngoài trời trong lúc mưa và ưu tiên nơi trong nhà. Trễ hay kẹt xe dưới 15 phút thì Journie tự dời lịch và báo bạn, có nút "Hoàn tác" (Undo), nếu bạn bật "Tự chỉnh thay đổi nhỏ" trong Hồ sơ. Chế độ "Mô phỏng" (Demo) cho thử luồng đổi lịch; chế độ "Vị trí thật" chưa có cảnh báo tự động vì máy chủ chưa có API phát hiện sự cố.

## Thời tiết | Weather

Thẻ thời tiết dùng dữ liệu thật từ Open-Meteo cho thành phố đang xem. Bầu trời trên màn hình tự chuyển sang ban đêm sau 18:30. Khi không có dữ liệu, Journie ghi "Chưa rõ thời tiết" thay vì đoán.

## Khám phá, gợi ý và bộ sưu tập | Discover, recommendations and sets

"Khám phá" (Discover) để tìm quán cà phê, bảo tàng... theo thành phố, có bộ lọc và sắp xếp; thả tim để lưu một nơi. "Gợi ý cho tôi" (Recommend): nói bạn muốn gì, Journie gợi ý những nơi hợp nhất; lưu lại thành bộ sưu tập hoặc lên lịch trình luôn. "Bộ sưu tập" (My sets) gom các nơi bạn thích thành từng bộ, rồi dùng một bộ để lên lịch trình. Mỗi nơi ghi giờ mở cửa và giá theo dữ liệu; thiếu thì hiện "Chưa rõ giờ" hoặc "Chưa rõ giá".

## Chia sẻ và ngôn ngữ | Sharing and languages

"Chia sẻ lịch trình" (Share the itinerary) tạo một liên kết; hiện liên kết chỉ mở được trên trình duyệt đang dùng vì máy chủ chưa có API chia sẻ. Journie có tiếng Việt và tiếng Anh: đổi bằng nút VI / EN trên thanh đầu trang.
