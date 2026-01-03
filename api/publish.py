"""
Airtable → Meta 自動發布 Webhook
Vercel Serverless Function

當 Airtable Automation 觸發時，接收 webhook 並發布到 Facebook/Instagram
"""

import os
import json
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.parse
import pytz

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
    url = f"https://graph.facebook.com/v21.0/{endpoint}"
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


def log_to_publishing_log(record_id, platform, action, post_id=None, error=None):
    """記錄到 Publishing_Log 表"""
    fields = {
        '時間': datetime.now().isoformat(),
        '內容': [record_id],  # Link to Contents record
        '平台': platform,
        '動作': action,  # "發布" 或 "排程"
    }
    if post_id:
        fields['Post_ID'] = post_id
    if error:
        fields['錯誤訊息'] = error

    airtable_request('POST', 'Publishing_Log', {'fields': fields})


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
    """處理單一記錄（含幂等性檢查）"""
    # 取得記錄
    record = airtable_request('GET', f"Contents/{record_id}")
    if not record:
        return {'error': 'Record not found'}

    fields = record.get('fields', {})

    # 幂等性檢查 1：已經發布過
    current_status = fields.get('發布狀態', '')
    if current_status in ['已發布', '已排程']:
        return {
            'skipped': True,
            'reason': f'Already {current_status}',
            'post_id': fields.get('FB_Post_ID')
        }

    # 幂等性檢查 2：已有 Post ID
    if fields.get('FB_Post_ID'):
        return {
            'skipped': True,
            'reason': 'Already has FB Post ID',
            'post_id': fields.get('FB_Post_ID')
        }

    content = fields.get('內容', '')
    platform = fields.get('發布平台', 'Facebook')
    scheduled_datetime = fields.get('發布時間')  # Airtable DateTime 欄位 (ISO 8601 格式)

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

    # 計算排程時間（解析 Airtable DateTime 欄位）
    scheduled_timestamp = None
    if scheduled_datetime:
        try:
            taiwan_tz = pytz.timezone('Asia/Taipei')

            # Airtable DateTime 欄位格式: "2026-01-05T14:00:00.000Z" 或 "2026-01-05T14:00:00"
            # 移除毫秒部分以便解析
            dt_str = scheduled_datetime.replace('.000Z', 'Z').replace('.000', '')

            # 解析 ISO 8601 格式
            if dt_str.endswith('Z'):
                # UTC 時間，需轉換為台灣時間理解
                dt_utc = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ")
                dt_utc = pytz.utc.localize(dt_utc)
                # 轉換到台灣時區檢查
                dt_taiwan = dt_utc.astimezone(taiwan_tz)
            else:
                # 假設是台灣時間（用戶在 UI 選的時間）
                dt_naive = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
                dt_taiwan = taiwan_tz.localize(dt_naive)

            # 轉換為 UTC timestamp
            scheduled_timestamp = int(dt_taiwan.timestamp())

            # 驗證時間範圍（至少 15 分鐘後）
            now_taipei = datetime.now(taiwan_tz)
            min_time = now_taipei + timedelta(minutes=15)

            if dt_taiwan < min_time:
                # 排程時間太近，改為立即發布
                scheduled_timestamp = None
                print(f"排程時間太近（{dt_taiwan.strftime('%Y-%m-%d %H:%M')}），改為立即發布")

        except Exception as e:
            print(f"發布時間解析錯誤: {e}, 原始值: {scheduled_datetime}")
            scheduled_timestamp = None

    # 發布
    result = {'record_id': record_id}

    if 'facebook' in platform.lower() or 'fb' in platform.lower():
        fb_result = publish_to_facebook(content, image_urls, scheduled_timestamp)
        result['facebook'] = fb_result

        if fb_result['success']:
            status = '已排程' if scheduled_timestamp else '已發布'
            action = '排程' if scheduled_timestamp else '發布'
            update_airtable_status(record_id, status, fb_id=fb_result.get('post_id'))
            log_to_publishing_log(record_id, 'Facebook', action, post_id=fb_result.get('post_id'))
        else:
            update_airtable_status(record_id, '發布失敗', error=fb_result.get('error'))
            log_to_publishing_log(record_id, 'Facebook', '發布', error=fb_result.get('error'))

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
