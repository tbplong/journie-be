# OSRM cho `POST /routing`

`/routing` lấy quãng đường đường bộ từ OSRM (dịch vụ `table`, một request cho cả ma trận N×N)
rồi tự tính thời gian: dưới 1 km đi bộ 4.5 km/h, còn lại đi xe 20 km/h. Server chỉ cần biến
môi trường `OSRM_URL`. Không đặt biến này, hoặc OSRM lỗi, thì API vẫn chạy: nó dùng đường chim
bay × 1.3 và trả `warnings: ["travel_estimated"]`.

## Dev và demo: server công khai

```
OSRM_URL=https://router.project-osrm.org
```

Không phải cài gì. Theo chính sách sử dụng của server demo: chỉ có profile ô tô, giới hạn
khoảng 1 request/giây, **không dùng cho production**.

## Production: tự host bằng Docker

Vercel không chạy được OSRM, nên OSRM là một máy riêng (VM hoặc container, ví dụ trên Azure,
đặt ở Đông Nam Á cho gần backend). Dữ liệu bản đồ: bản Việt Nam của Geofabrik (OpenStreetMap,
license ODbL).

```powershell
mkdir osrm; cd osrm
curl.exe -LO https://download.geofabrik.de/asia/vietnam-latest.osm.pbf

$img = "ghcr.io/project-osrm/osrm-backend"   # nên ghim một tag release cụ thể thay vì latest
docker run -t -v "${PWD}:/data" $img osrm-extract -p /opt/car.lua /data/vietnam-latest.osm.pbf
docker run -t -v "${PWD}:/data" $img osrm-partition /data/vietnam-latest.osrm
docker run -t -v "${PWD}:/data" $img osrm-customize /data/vietnam-latest.osrm
docker run -d -p 5000:5000 -v "${PWD}:/data" --restart unless-stopped $img `
  osrm-routed --algorithm mld /data/vietnam-latest.osrm
```

Kiểm tra (Bến Thành → Nhà hát Thành phố, thứ tự `lon,lat`):

```powershell
curl.exe "http://localhost:5000/table/v1/driving/106.698,10.7725;106.7009,10.7769?annotations=distance"
```

Kết quả có `"code":"Ok"` và `distances` tính bằng mét. Sau đó đặt `OSRM_URL=http://<host>:5000`
cho backend.

Ghi chú:

- `osrm-routed` mặc định nhận tối đa 100 toạ độ mỗi request `table`. `/routing` gửi tối đa
  51 (50 địa điểm + điểm xuất phát), nên không cần đổi.
- Cập nhật bản đồ: tải lại file `.osm.pbf` rồi chạy lại ba bước extract, partition, customize.
- Chưa đo RAM và thời gian dựng dữ liệu cho bản Việt Nam; hãy đo ở lần dựng đầu tiên trước khi
  chọn cỡ máy.
- OSRM không có profile xe máy; profile ô tô chỉ dùng để lấy quãng đường. Muốn sát hơn thì
  viết profile Lua riêng, chuyện của sau này.
- Cổng 5000 không có xác thực: chỉ mở cho backend (firewall hoặc mạng nội bộ), không mở ra
  Internet.
