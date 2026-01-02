"""
Airtable → Meta 自動發布 Webhook
Vercel Serverless Function

當 Airtable Automation 觸發時，接收 webhook 並發布到 Facebook/Instagram
"""

import os
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.parse

# 環境變數（在 Vercel 設定）
AIRTABLE_TOKEN = os.environ.get('AIRTABLE_TOKEN')
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID', 'appx62WWzcQhOIRpr')
META_PAGE_ID = os.environ.get('META_PAGE_ID')
META_PAGE_TOKEN = os.environ.get('META_PAGE_TOKEN')
META_IG_ACCOUNT_ID = os.environ.get('META_IG_ACCOUNT_ID')
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')  # 可選：驗證 webhook 來源


def airtable_request(method, endpoint, data=None):
    """Airtable API 請求"""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{endpoint}"
    headers = {
        'Authorization': f'Bearer {AIRTABLE_TOKEN}',
        'Content-Type': 'application/json'
    }

    req = urllib.request.Request(url, headers=headers, method=method)
    if data:
        req.data = json.dumps(data).encode('utf-8')

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        print(f"Airtable error: {e}")
        return None


def meta_request(endpoint, data):
    """Meta Graph API 請求"""
    url = f"https://graph.facebook.com/v18.0/{endpoint}"
    data['access_token'] = META_PAGE_TOKEN

    post_data = urllib.parse.urlencode(data).encode('utf-8')
    req = urllib.request.Request(url, data=post_data, method='POST')

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode('utf-8')
        print(f"Meta API error: {error_body}")
        return json.loads(error_body)
    except Exception as e:
        print(f"Meta request error: {e}")
        return {'error': {'message': str(e)}}


def drive_url_to_direct(url):
    """Google Drive URL 轉直接下載連結"""
    if '/file/d/' in url:
        file_id = url.split('/file/d/')[1].split('/')[0]
        return f"https://drive.google.com/uc?export=download&id={file_id}"
    return url


def get_image_url(url):
    """取得可直接訪問的圖片 URL"""
    if not url:
        return url
    # Airtable CDN URL 可直接使用
    if 'airtableusercontent.com' in url:
        return url
    # Google Drive URL 需要轉換
    if 'drive.google.com' in url:
        return drive_url_to_direct(url)
    return url


def publish_to_facebook(content, image_urls, scheduled_timestamp=None):
    """發布到 Facebook"""
    if not META_PAGE_ID or not META_PAGE_TOKEN:
        return {'success': False, 'error': 'Missing META_PAGE_ID or META_PAGE_TOKEN'}

    try:
        if len(image_urls) == 1:
            # 單圖
            data = {
                'url': get_image_url(image_urls[0]),
                'caption': content,
            }
            if scheduled_timestamp:
                data['published'] = 'false'
                data['scheduled_publish_time'] = str(scheduled_timestamp)

            result = meta_request(f"{META_PAGE_ID}/photos", data)

        else:
            # 多圖
            photo_ids = []
            for img_url in image_urls[:10]:
                result = meta_request(f"{META_PAGE_ID}/photos", {
                    'url': get_image_url(img_url),
                    'published': 'false'
                })
                if 'id' in result:
                    photo_ids.append({'media_fbid': result['id']})

            if not photo_ids:
                return {'success': False, 'error': 'Failed to upload images'}

            data = {
                'message': content,
                'attached_media': json.dumps(photo_ids),
            }
            if scheduled_timestamp:
                data['published'] = 'false'
                data['scheduled_publish_time'] = str(scheduled_timestamp)

            result = meta_request(f"{META_PAGE_ID}/feed", data)

        if 'id' in result:
            return {
                'success': True,
                'post_id': result['id'],
                'permalink': f"https://www.facebook.com/{result['id']}"
            }
        else:
            return {
                'success': False,
                'error': result.get('error', {}).get('message', str(result))
            }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def update_airtable_status(record_id, status, fb_id=None, error=None):
    """更新 Airtable 記錄狀態"""
    fields = {
        '發布狀態': status,
        '確認發布': False  # 取消勾選，避免重複觸發
    }
    if fb_id:
        fields['FB_Post_ID'] = fb_id
    if error:
        fields['拒絕原因'] = error

    airtable_request('PATCH', f"Contents/{record_id}", {'fields': fields})


def process_record(record_id):
    """處理單一記錄"""
    # 取得記錄
    record = airtable_request('GET', f"Contents/{record_id}")
    if not record:
        return {'error': 'Record not found'}

    fields = record.get('fields', {})
    content = fields.get('內容', '')
    platform = fields.get('發布平台', 'Facebook')
    scheduled_date = fields.get('排程日期')
    scheduled_time = fields.get('排程時間', '10:00')

    # 取得圖片 - 直接使用「圖片預覽」attachment 的所有圖片
    image_urls = []
    preview_attachments = fields.get('圖片預覽', [])

    for attachment in preview_attachments:
        url = attachment.get('url')
        if url:
            image_urls.append(url)

    if not image_urls:
        update_airtable_status(record_id, '發布失敗', error='沒有可用的圖片')
        return {'error': 'No images available'}

    # 計算排程時間（用戶輸入的是台灣時間 UTC+8）
    scheduled_timestamp = None
    if scheduled_date:
        try:
            dt_str = f"{scheduled_date} {scheduled_time or '10:00'}"
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            # 用戶輸入台灣時間，轉換為 UTC timestamp
            # 台灣時間 = UTC + 8，所以 UTC = 台灣時間 - 8 小時
            from datetime import timezone, timedelta
            taiwan_tz = timezone(timedelta(hours=8))
            dt_taiwan = dt.replace(tzinfo=taiwan_tz)
            scheduled_timestamp = int(dt_taiwan.timestamp())
        except:
            pass

    # 發布
    result = {'record_id': record_id}

    if 'facebook' in platform.lower() or 'fb' in platform.lower():
        fb_result = publish_to_facebook(content, image_urls, scheduled_timestamp)
        result['facebook'] = fb_result

        if fb_result['success']:
            status = '已排程' if scheduled_timestamp else '已發布'
            update_airtable_status(record_id, status, fb_id=fb_result.get('post_id'))
        else:
            update_airtable_status(record_id, '發布失敗', error=fb_result.get('error'))

    return result


class handler(BaseHTTPRequestHandler):
    """Vercel Serverless Function Handler"""

    def do_POST(self):
        # 讀取請求內容
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            data = json.loads(body) if body else {}
        except:
            data = {}

        # 驗證 webhook secret（可選）
        if WEBHOOK_SECRET:
            auth = self.headers.get('Authorization', '')
            if auth != f'Bearer {WEBHOOK_SECRET}':
                self.send_response(401)
                self.end_headers()
                self.wfile.write(b'Unauthorized')
                return

        # 取得 record_id
        record_id = data.get('record_id')

        if not record_id:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': 'Missing record_id'}).encode())
            return

        # 處理發布
        result = process_record(record_id)

        # 回應
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def do_GET(self):
        """健康檢查"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({
            'status': 'ok',
            'service': 'airtable-meta-webhook',
            'timestamp': datetime.now().isoformat()
        }).encode())
