# app.py
import io
import uuid
import base64
from datetime import datetime

import streamlit as st
import streamlit.components.v1 as components

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload


# =========================
# 設定
# =========================
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

REQUIRED_IMAGE_TYPES = ["ロゴ", "馬車タグ", "製造国タグ", "YKK"]

LEARN_NO_REASONS = [
    "画像品質不良（ピント/反射/暗い）",
    "必要情報が写っていない",
    "基準未確定／判断が割れる",
    "テスト・検証用データ",
    "その他（自由記述で補足）",
]

# 画像タイプごとの判定理由（絞り込み）
REASONS_BY_TYPE = {
    "ロゴ": [
        "ロゴ：フォント／配置／刻印が基準内",
        "ロゴ：にじみ／ズレ／形状違い",
    ],
    "馬車タグ": [
        "馬車タグ：ピッチが5/7で基準内",
        "馬車タグ：ピッチが基準外（5/7以外）",
        "馬車タグ：キャビン形状が基準内",
        "馬車タグ：キャビン形状が基準外",
    ],
    "製造国タグ": [
        "製造国タグ：印刷／フォントが自然",
        "製造国タグ：にじみ／ズレ／フォント異常",
    ],
    "YKK": [
        "YKK：刻印が深く均一",
        "YKK：刻印が浅い／欠け／潰れ",
    ],
}
COMMON_REASON_ALWAYS = "判別不可（画像不鮮明）"


THUMB_WIDTH_PX = 280       # サムネ幅（固定）
ZOOM_HEIGHT_PX = 650       # ★ズーム枠の高さ（見やすさ優先）


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_clients():
    sa_info = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(sa_info, scopes=SCOPES)

    gc = gspread.authorize(creds)
    drive = build("drive", "v3", credentials=creds)
    return gc, drive


def ensure_folder(drive, name: str, parent_id: str) -> str:
    q = (
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{name}' and "
        f"'{parent_id}' in parents and "
        "trashed=false"
    )

    res = drive.files().list(
        q=q,
        fields="files(id,name)",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
    ).execute()

    files = res.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }

    folder = drive.files().create(
        body=metadata,
        fields="id",
        supportsAllDrives=True,
    ).execute()

    return folder["id"]


def upload_image_to_drive(drive, parent_folder_id: str, filename: str, data: bytes, mimetype: str):
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=False)
    file_metadata = {"name": filename, "parents": [parent_folder_id]}

    f = drive.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()

    return f["id"], f.get("webViewLink", "")


def open_worksheets(gc, spreadsheet_id: str):
    sh = gc.open_by_key(spreadsheet_id)
    titles = [ws.title for ws in sh.worksheets()]

    if "Cases" not in titles:
        ws = sh.add_worksheet(title="Cases", rows=1000, cols=30)
        ws.append_row([
            "case_id", "brand", "item", "judge_person", "memo",
            "images_count", "overall_judge", "overall_reason_choices", "overall_reason_free",
            "overall_learn_yn", "overall_learn_no_reason", "weight_version", "created_at"
        ])

    if "Images" not in titles:
        ws = sh.add_worksheet(title="Images", rows=5000, cols=30)
        ws.append_row([
            "case_id", "image_type", "drive_file_id", "drive_view_url",
            "judge", "reason_choices", "reason_free",
            "learn_yn", "learn_no_reason", "created_at"
        ])

    return sh.worksheet("Cases"), sh.worksheet("Images")


def reason_options(image_type: str):
    base = REASONS_BY_TYPE.get(image_type, [])
    # 画像タイプに関係なく「判別不可」は出す
    return base + [COMMON_REASON_ALWAYS]


# =========================
# ズームビューア（アプリ内で拡大・ドラッグ移動）
# =========================
def zoom_viewer(image_bytes: bytes, mimetype: str, height: int = 650):
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    html = r"""
<div style="font-family: sans-serif;">
  <div style="display:flex; gap:8px; align-items:center; margin-bottom:8px;">
    <button id="zin" style="padding:6px 10px;">＋</button>
    <button id="zout" style="padding:6px 10px;">－</button>
    <button id="zreset" style="padding:6px 10px;">リセット</button>
    <span style="opacity:0.8;">（ドラッグで移動 / ボタンで拡大）</span>
  </div>

  <div id="wrap" style="width:100%; height:__H__px; overflow:hidden; border-radius:12px; border:1px solid rgba(255,255,255,0.15); background:rgba(0,0,0,0.25); position:relative;">
    <img id="img" src="data:__MIME__;base64,__B64__" style="transform-origin: 0 0; cursor:grab; user-select:none; -webkit-user-drag:none; position:absolute; left:0; top:0;" />
  </div>
</div>

<script>
  const img = document.getElementById("img");
  const wrap = document.getElementById("wrap");
  const zin = document.getElementById("zin");
  const zout = document.getElementById("zout");
  const zreset = document.getElementById("zreset");

  let scale = 1.0;
  let x = 0;
  let y = 0;
  let dragging = false;
  let lastX = 0;
  let lastY = 0;

  function apply() {
    img.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
  }

  function reset() {
    scale = 1.0;
    x = 0;
    y = 0;
    apply();
  }

  zin.onclick = () => {
    scale = Math.min(scale * 1.25, 8);
    apply();
  };
  zout.onclick = () => {
    scale = Math.max(scale / 1.25, 1);
    apply();
  };
  zreset.onclick = () => reset();

  wrap.addEventListener("mousedown", (e) => {
    dragging = true;
    img.style.cursor = "grabbing";
    lastX = e.clientX;
    lastY = e.clientY;
  });
  window.addEventListener("mouseup", () => {
    dragging = false;
    img.style.cursor = "grab";
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - lastX;
    const dy = e.clientY - lastY;
    x += dx;
    y += dy;
    lastX = e.clientX;
    lastY = e.clientY;
    apply();
  });

  reset();
</script>
"""
    html = html.replace("__B64__", b64).replace("__MIME__", mimetype).replace("__H__", str(height))
    components.html(html, height=height + 90, scrolling=False)


# =========================
# 次のアイテム（判定者だけ残す）＋トップへ戻す
# =========================
def new_form_keep_judge_person():
    keep = st.session_state.get("judge_person", "")
    st.session_state["form_id"] = st.session_state.get("form_id", 0) + 1
    st.session_state["judge_person"] = keep
    st.session_state["scroll_top"] = True  # ★トップへ戻すフラグ
    st.rerun()


# =========================
# UI
# =========================
st.set_page_config(page_title="COACH 育成中専用画面", layout="wide")

# ★トップへ戻す（rerun後に実行される）
if st.session_state.get("scroll_top"):
    components.html("<script>window.scrollTo(0,0);</script>", height=0)
    st.session_state["scroll_top"] = False

st.title("COACH 真贋判定 - 育成中専用画面（Training Console）")

form_id = st.session_state.get("form_id", 0)

col1, col2, col3 = st.columns(3)
with col1:
    st.text_input("ブランド", value="COACH", disabled=True, key=f"{form_id}_brand")
with col2:
    item = st.selectbox("アイテム", ["バッグ", "財布"], key=f"{form_id}_item")
with col3:
    st.text_input("判定者（判定士名）", placeholder="例：柴田", key="judge_person")

memo = st.text_area("メモ（任意）", placeholder="気づいたことがあれば", key=f"{form_id}_memo")

st.divider()
st.subheader("写真（1〜4枚）")
st.caption("※ 画像タイプは必須、同一タイプは1枚まで。最初は1枚だけでも保存できます。")

img_count = st.number_input("登録する写真枚数", min_value=1, max_value=4, value=1, step=1, key=f"{form_id}_img_count")

chosen_types = []
images_payload = []

for i in range(int(img_count)):
    st.markdown(f"### 写真 {i+1}")
    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])

    with c1:
        uploaded = st.file_uploader(
            f"画像アップロード（写真{i+1}）",
            type=["jpg", "jpeg", "png"],
            key=f"{form_id}_up_{i}"
        )

    with c2:
        available = [t for t in REQUIRED_IMAGE_TYPES if t not in chosen_types]
        image_type = st.selectbox("画像タイプ（必須）", options=available, key=f"{form_id}_type_{i}")
        chosen_types.append(image_type)

    with c3:
        judge = st.selectbox("判定（必須）", ["基準内", "基準外", "判断つかず"], key=f"{form_id}_judge_{i}")

    with c4:
        learn_yn = st.radio("学習（必須）", ["Yes", "No"], horizontal=True, key=f"{form_id}_learn_{i}")

    if uploaded is not None:
        file_bytes = uploaded.getvalue()
        mimetype = uploaded.type or "image/jpeg"

        left, right = st.columns([1, 3])
        with left:
            st.markdown("**サムネ**")
            st.image(file_bytes, width=THUMB_WIDTH_PX, caption=f"{image_type}")
        with right:
            with st.expander("ズームして確認（アプリ内）", expanded=False):
                zoom_viewer(file_bytes, mimetype=mimetype, height=ZOOM_HEIGHT_PX)

    # ★画像タイプに応じて、判定理由を絞り込み
    reason_choices = st.multiselect(
        "判定理由（選択肢・複数OK）",
        options=reason_options(image_type),
        key=f"{form_id}_choices_{i}",
    )
    reason_free = st.text_input("判定理由（自由記述）", key=f"{form_id}_free_{i}", placeholder="例：ピッチが5/7ではないため")

    learn_no_reason = ""
    if learn_yn == "No":
        learn_no_reason = st.selectbox("学習No理由（必須）", LEARN_NO_REASONS, key=f"{form_id}_no_reason_{i}")
        if "その他" in learn_no_reason:
            learn_no_reason += "：" + (reason_free or "（自由記述に補足してください）")

    images_payload.append({
        "uploaded": uploaded,
        "image_type": image_type,
        "judge": judge,
        "reason_choices": " / ".join(reason_choices),
        "reason_free": reason_free,
        "learn_yn": learn_yn,
        "learn_no_reason": learn_no_reason,
    })

    st.divider()

overall = None
if int(img_count) >= 3:
    st.subheader("総合判定（写真3枚以上のとき）")
    oc1, oc2, oc3 = st.columns([1, 2, 1])
    with oc1:
        overall_judge = st.selectbox("総合判定", ["基準内", "基準外", "判断つかず"], key=f"{form_id}_overall_j")
    with oc2:
        overall_reason_choices = st.multiselect(
            "総合理由（選択肢・複数OK）",
            options=[
                "馬車タグが基準内のため総合は基準内寄り",
                "最重要ポイント（馬車タグ）が基準外のため総合は基準外寄り",
                "情報不足のため総合判断つかず",
                "複合的に判断（基準内要素が優勢）",
                "複合的に判断（基準外要素が優勢）",
            ],
            key=f"{form_id}_overall_choices",
        )
    with oc3:
        overall_learn_yn = st.radio("総合 学習", ["Yes", "No"], horizontal=True, key=f"{form_id}_overall_learn")

    overall_reason_free = st.text_input("総合理由（自由記述）", key=f"{form_id}_overall_free", placeholder="例：馬車タグが基準内で、他は軽微のため")
    overall_learn_no_reason = ""
    if overall_learn_yn == "No":
        overall_learn_no_reason = st.selectbox("総合 学習No理由（必須）", LEARN_NO_REASONS, key=f"{form_id}_overall_no_reason")

    overall = {
        "overall_judge": overall_judge,
        "overall_reason_choices": " / ".join(overall_reason_choices),
        "overall_reason_free": overall_reason_free,
        "overall_learn_yn": overall_learn_yn,
        "overall_learn_no_reason": overall_learn_no_reason,
    }

st.divider()

if st.button("保存（Drive + Sheets）", type="primary", key=f"{form_id}_save"):
    judge_person = st.session_state.get("judge_person", "").strip()
    if not judge_person:
        st.error("判定者（判定士名）を入力してください。")
        st.stop()

    for idx, p in enumerate(images_payload):
        if p["uploaded"] is None:
            st.error(f"写真{idx+1}の画像が未選択です。")
            st.stop()

    spreadsheet_id = st.secrets["app"]["spreadsheet_id"]
    drive_root_folder_id = st.secrets["app"]["drive_root_folder_id"]
    weight_version = st.secrets["app"].get("weight_version", "COACH_v1.0")

    gc, drive = get_clients()
    ws_cases, ws_images = open_worksheets(gc, spreadsheet_id)

    case_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    created_at = now_str()

    case_folder_id = ensure_folder(drive, case_id, drive_root_folder_id)

    for p in images_payload:
        up = p["uploaded"]
        file_bytes = up.getvalue()
        filename = f"{p['image_type']}_{up.name}"
        mimetype = up.type or "image/jpeg"

        file_id, view_url = upload_image_to_drive(drive, case_folder_id, filename, file_bytes, mimetype)

        ws_images.append_row([
            case_id,
            p["image_type"],
            file_id,
            view_url,
            p["judge"],
            p["reason_choices"],
            p["reason_free"],
            p["learn_yn"],
            p["learn_no_reason"],
            created_at,
        ])

    if overall is None:
        ws_cases.append_row([
            case_id, "COACH", item, judge_person, st.session_state.get(f"{form_id}_memo", ""),
            int(img_count), "", "", "",
            "", "", weight_version, created_at
        ])
    else:
        ws_cases.append_row([
            case_id, "COACH", item, judge_person, st.session_state.get(f"{form_id}_memo", ""),
            int(img_count),
            overall["overall_judge"],
            overall["overall_reason_choices"],
            overall["overall_reason_free"],
            overall["overall_learn_yn"],
            overall["overall_learn_no_reason"],
            weight_version,
            created_at
        ])

    st.success(f"保存しました！ case_id = {case_id}")

    st.markdown("### 次の操作")
    if st.button("次のアイテムを真贋する（入力クリア）", key=f"{form_id}_next"):
        new_form_keep_judge_person()

st.divider()
with st.expander("ふっかつの呪文 / バージョン（管理用）", expanded=False):
    st.markdown("**Ver：BV-COACH-MVP-3.4**")
    st.markdown("**呪文：**「**ずーむたかさあっぷ・くりあしててっぺんにもどる・りゆうはたいぷでしぼる**」")
