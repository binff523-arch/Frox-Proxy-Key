# FROX PROXY

Web app cấp key ngẫu nhiên, quản lý key phía server (Flask) để mọi người dùng thấy cùng một trạng thái key.

## Cấu trúc file
```
frox-proxy/
├── app.py              # Backend Flask (logic lấy key, API)
├── templates/
│   └── index.html      # Giao diện web
├── requirements.txt    # Thư viện cần cài
├── Procfile             # Lệnh khởi chạy cho Render
└── render.yaml          # Cấu hình deploy tự động (tùy chọn)
```

## 1. Đưa lên GitHub
```bash
cd frox-proxy
git init
git add .
git commit -m "Init FROX PROXY"
git branch -M main
git remote add origin https://github.com/<username>/<ten-repo>.git
git push -u origin main
```

## 2. Deploy lên Render
1. Vào https://render.com → **New** → **Web Service**
2. Chọn **Build and deploy from a Git repository**, kết nối repo GitHub vừa tạo
3. Render sẽ tự đọc `render.yaml` (nếu có). Nếu không, điền thủ công:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`
   - **Environment**: Python 3
4. Nhấn **Create Web Service** và đợi build xong
5. Render sẽ cấp một URL dạng `https://frox-proxy.onrender.com` — truy cập là dùng được ngay

## Cơ chế thời hạn key
- Mỗi key khi được cấp có hạn dùng **24 giờ** kể từ lúc lấy.
- Sau 24h, key tự động bị thu hồi và trả lại vào kho để cấp cho lượt sau (kiểm tra mỗi khi có người gọi API).
- API `GET /api/check-key/<key>` dùng để kiểm tra 1 key còn hạn hay không.

## Cơ chế vượt link4m (1 lần / 24h)
- Người dùng phải **vượt link4m 1 lần** trước khi lấy key. Sau khi vượt xong, hệ thống nhớ trạng thái này (qua session cookie) trong **24 giờ** — trong thời gian đó, bấm "LẤY KEY NGAY" bao nhiêu lần cũng được, không cần vượt lại.
- Luồng hoạt động:
  1. Bấm nút → gọi `/api/start-verify` → server tạo link rút gọn qua link4m trỏ về `/verify?token=...`
  2. Trình duyệt chuyển sang link4m, người dùng vượt quảng cáo
  3. Link4m tự động redirect về `/verify?token=...` trên site của bạn → server xác nhận và đánh dấu đã vượt (24h)
  4. Quay lại trang chủ, tự động lấy key luôn

### Cấu hình biến môi trường (khuyến nghị khi deploy)
Trên Render, vào **Environment** và thêm các biến sau (để không lộ token API khi đẩy code lên GitHub public):
| Biến | Ý nghĩa |
|---|---|
| `LINK4M_API_TOKEN` | API token tài khoản link4m của bạn (xem trong Developers API trên my.link4m.com) |
| `SECRET_KEY` | Chuỗi bí mật ngẫu nhiên bất kỳ để mã hóa session cookie |

Nếu không đặt `LINK4M_API_TOKEN`, code sẽ dùng token mặc định đã gán sẵn trong `app.py` — **nên đổi sang biến môi trường nếu repo GitHub là public** để tránh lộ token.

## Ghi chú
- Trạng thái key + trạng thái vượt link hiện lưu trong bộ nhớ (in-memory) / session cookie của server — khi server restart (Render free tier có thể ngủ sau 15 phút không dùng) thì key và trạng thái vượt link sẽ reset lại từ đầu.
- Muốn key không bị mất khi restart, cần lưu vào database (ví dụ Render PostgreSQL hoặc Redis) — có thể yêu cầu nâng cấp sau.
