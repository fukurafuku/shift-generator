import io
from datetime import date, datetime, timedelta
import holidays
from ortools.sat.python import cp_model
import pandas as pd
import streamlit as st

WEEKDAYS_JP = ['月', '火', '水', '木', '金', '土', '日']


# ------------------------------------------------------------
# 日付・設定読み込み処理
# ------------------------------------------------------------
def weekday_name(d):
  return WEEKDAYS_JP[d.weekday()]


def parse_date_flexible(value, default_year=2026):
  if isinstance(value, (datetime, date)):
    return value if isinstance(value, date) else value.date()
  val_str = str(value).strip()
  try:
    dt = pd.to_datetime(val_str)
    if pd.notna(dt):
      return dt.date()
  except Exception:
    pass
  try:
    if '/' in val_str:
      parts = val_str.split('/')
      return date(default_year, int(parts[0]), int(parts[1]))
  except Exception:
    pass
  raise ValueError(f'日付形式エラー: {val_str}')


def get_japanese_holidays(dates):
  if not dates:
    return set()
  years = sorted(set(d.year for d in dates))
  all_holidays = set()
  for y in years:
    for h_date in holidays.JP(years=y):
      all_holidays.add(h_date)
  return {d for d in dates if d in all_holidays}


def is_maru(val):
  """'○', '〇', 'o', 'O' などの記号を判定"""
  if pd.isna(val):
    return False
  s = str(val).strip()
  return s in ['○', '〇', 'o', 'O', '1', 'True']


def load_fixed_rules_from_excel_bytes(excel_bytes, sheet_name='固定ルール'):
  excel_file = io.BytesIO(excel_bytes)
  df_raw = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)

  col_map = {'月': 3, '火': 4, '水': 5, '木': 6, '金': 7, '土': 8, '日': 9}

  # 必要人数の初期化（曜日ごとの辞書構造）
  req_min = {
      'QUALIFIED_TOTAL': {w: 0 for w in WEEKDAYS_JP},
      'QUALIFIED_EARLY': {w: None for w in WEEKDAYS_JP},  # Noneは「早遅指定なし」
      'QUALIFIED_LATE': {w: None for w in WEEKDAYS_JP},
      'UNQUALIFIED': {w: 0 for w in WEEKDAYS_JP},
  }

  for r in range(len(df_raw)):
    label = str(df_raw.iloc[r, 1]).strip() if pd.notna(df_raw.iloc[r, 1]) else ''

    if '資格者合計' in label:
      for w_key, col_i in col_map.items():
        if col_i < len(df_raw.columns) and pd.notna(df_raw.iloc[r, col_i]):
          try:
            req_min['QUALIFIED_TOTAL'][w_key] = int(df_raw.iloc[r, col_i])
          except ValueError:
            pass

    elif 'うち資格者早番' in label:
      for w_key, col_i in col_map.items():
        if col_i < len(df_raw.columns) and pd.notna(df_raw.iloc[r, col_i]):
          val_str = str(df_raw.iloc[r, col_i]).strip()
          if val_str.isdigit():
            req_min['QUALIFIED_EARLY'][w_key] = int(val_str)

    elif 'うち資格者遅番' in label:
      for w_key, col_i in col_map.items():
        if col_i < len(df_raw.columns) and pd.notna(df_raw.iloc[r, col_i]):
          val_str = str(df_raw.iloc[r, col_i]).strip()
          if val_str.isdigit():
            req_min['QUALIFIED_LATE'][w_key] = int(val_str)

    elif '一般スタッフ' in label:
      for w_key, col_i in col_map.items():
        if col_i < len(df_raw.columns) and pd.notna(df_raw.iloc[r, col_i]):
          try:
            req_min['UNQUALIFIED'][w_key] = int(df_raw.iloc[r, col_i])
          except ValueError:
            pass

  # スタッフ一覧ヘッダー行の検索
  header_row_idx = None
  for idx, row in df_raw.iterrows():
    row_vals = [str(v).strip() for v in row.values if pd.notna(v)]
    if 'スタッフ一覧' in row_vals or 'スタッフ' in row_vals:
      header_row_idx = idx
      break

  excel_file.seek(0)
  df_staff = pd.read_excel(
      excel_file, sheet_name=sheet_name, skiprows=header_row_idx
  )
  df_staff.columns = [str(c).strip() for c in df_staff.columns]

  # 列名の特定
  staff_col = [c for c in df_staff.columns if 'スタッフ' in c][0]
  type_col = [c for c in df_staff.columns if '区分' in c][0]
  job_col = [c for c in df_staff.columns if '職業' in c][0]
  early_col = [c for c in df_staff.columns if '早番' in c][0]
  late_col = [c for c in df_staff.columns if '遅番' in c][0]
  max_c_col = [c for c in df_staff.columns if '最大連勤' in c][0]

  staff_list, full_time, part_time = [], [], []
  qualified_staff, unqualified_staff = [], []
  early_staff, late_staff = [], []
  fixed_holidays, max_consecutive = {}, {}

  for _, row in df_staff.iterrows():
    s_val = str(row[staff_col]).strip()
    if not s_val or s_val in ['nan', 'None', '']:
      continue
    staff_list.append(s_val)

    if '正社員' in str(row[type_col]):
      full_time.append(s_val)
    else:
      part_time.append(s_val)

    job = str(row[job_col]).strip()
    if any(q in job for q in ['薬剤師', '登録販売者', '資格者']):
      qualified_staff.append(s_val)
    else:
      unqualified_staff.append(s_val)

    # 早番・遅番の判定（○がついているか）
    if is_maru(row[early_col]):
      early_staff.append(s_val)
    if is_maru(row[late_col]):
      late_staff.append(s_val)

    # 曜日ごとの出勤可能日（○がついていない曜日は固定休日）
    off_days = []
    for w_key in WEEKDAYS_JP:
      w_cols = [c for c in df_staff.columns if c == w_key]
      if w_cols:
        if not is_maru(row[w_cols[0]]):
          off_days.append(w_key)
    fixed_holidays[s_val] = off_days

    # 最大連勤数
    mc = str(row[max_c_col]).strip()
    if mc.isdigit():
      max_consecutive[s_val] = int(mc)

  return {
      'STAFF': staff_list,
      'FULL_TIME': full_time,
      'PART_TIME': part_time,
      'QUALIFIED_STAFF': qualified_staff,
      'UNQUALIFIED_STAFF': unqualified_staff,
      'EARLY_STAFF': early_staff,
      'LATE_STAFF': late_staff,
      'FIXED_HOLIDAYS': fixed_holidays,
      'MAX_CONSECUTIVE': max_consecutive,
      'REQ_MIN': req_min,
  }


# ------------------------------------------------------------
# シフト生成処理
# ------------------------------------------------------------
def generate_shift_from_bytes(excel_bytes):
  rules = load_fixed_rules_from_excel_bytes(excel_bytes, sheet_name='固定ルール')

  excel_file = io.BytesIO(excel_bytes)

  # カレンダー入力シートの読み込み（シート名の揺れに対応）
  cal_sheet_name = 'カレンダー入力'
  xl_obj = pd.ExcelFile(excel_file)
  for s_name in xl_obj.sheet_names:
    if 'カレンダー' in s_name:
      cal_sheet_name = s_name
      break

  excel_file.seek(0)
  df_cal = pd.read_excel(excel_file, sheet_name=cal_sheet_name, header=None)

  holiday_requests, extra_work = {}, {}
  dates_list = []

  # ヘッダー行（スタッフ名が書かれている行）を探索
  header_row_idx = None
  staff_cols = {}
  for r_idx in range(min(15, len(df_cal))):
    row_str_vals = [
        str(val).strip() for val in df_cal.iloc[r_idx].values if pd.notna(val)
    ]
    # スタッフ名が含まれている行を探す
    for s_name in rules['STAFF']:
      if any(s_name in v for v in row_str_vals):
        header_row_idx = r_idx
        break
    if header_row_idx is not None:
      break

  if header_row_idx is not None:
    for c_idx in range(len(df_cal.columns)):
      val = str(df_cal.iloc[header_row_idx, c_idx]).strip()
      for s_name in rules['STAFF']:
        if s_name in val or val in s_name:
          staff_cols[s_name] = c_idx

  # 日付列の探索（左側3〜5列目周辺から日付を探す）
  date_col_idx = 3
  for c_idx in range(min(5, len(df_cal.columns))):
    col_vals = df_cal.iloc[:, c_idx].dropna().tolist()
    for v in col_vals:
      try:
        parse_date_flexible(v)
        date_col_idx = c_idx
        break
      except Exception:
        continue

  start_row = (header_row_idx + 1) if header_row_idx is not None else 4
  for r in range(start_row, len(df_cal)):
    d_val = df_cal.iloc[r, date_col_idx]
    if pd.isna(d_val) or str(d_val).strip() in ['', 'nan', 'None']:
      continue
    try:
      d_obj = parse_date_flexible(d_val)
    except ValueError:
      continue

    d_str = d_obj.strftime('%Y-%m-%d')
    dates_list.append(d_obj)
    holiday_requests[d_str], extra_work[d_str] = [], []

    for s_name, col_idx in staff_cols.items():
      if col_idx < len(df_cal.columns):
        cell_val = (
            str(df_cal.iloc[r, col_idx]).strip()
            if pd.notna(df_cal.iloc[r, col_idx])
            else ''
        )
        # 研修の文字列チェック（「研」「研修」など柔軟に読み込み）
        if cell_val in ['公休', '休', '希望休', '×']:
          holiday_requests[d_str].append(s_name)
        elif '研' in cell_val:
          extra_work[d_str].append(s_name)

  dates_list.sort()
  dates = [
      dates_list[0] + timedelta(days=i)
      for i in range((dates_list[-1] - dates_list[0]).days + 1)
  ]
  jp_holidays = get_japanese_holidays(dates)

  model = cp_model.CpModel()
  shifts = {
      (s, d): model.NewBoolVar(f'shift_{s}_{d}')
      for s in rules['STAFF']
      for d in dates
  }

  deficiency_q_vars, deficiency_unqual_vars = {}, {}
  penalty_terms = []

  # 1. 休・出勤・研修条件の設定
  for d in dates:
    w_name = weekday_name(d)
    is_j_holiday = d in jp_holidays
    d_str = d.strftime('%Y-%m-%d')

    for s in rules['STAFF']:
      is_extra = s in extra_work.get(d_str, [])
      is_req_off = s in holiday_requests.get(d_str, [])
      is_fixed_off = w_name in rules['FIXED_HOLIDAYS'].get(s, [])

      if s in rules['PART_TIME']:
        if (is_j_holiday or is_fixed_off or is_req_off) and not is_extra:
          model.Add(shifts[(s, d)] == 0)
        else:
          model.Add(shifts[(s, d)] == 1)
      else:  # 正社員
        if is_extra:
          model.Add(shifts[(s, d)] == 1)  # 研修日は強制出勤扱い
        elif is_req_off or is_j_holiday or is_fixed_off:
          model.Add(shifts[(s, d)] == 0)

  # 2. 正社員の公休数確保
  off_days_fulltime = {s: 10 for s in rules['FULL_TIME']}
  for s in rules['FULL_TIME']:
    req_off = off_days_fulltime.get(s, 10)
    total_off = sum(1 - shifts[(s, d)] for d in dates)
    model.Add(total_off >= req_off)
    penalty_terms.append(total_off * 10)

  # 3. 最大連勤制限
  for s, max_c in rules['MAX_CONSECUTIVE'].items():
    for i in range(len(dates) - max_c):
      model.Add(
          sum(shifts[(s, dates[i + j])] for j in range(max_c + 1)) <= max_c
      )

  # 研修日の人は「店舗の通常人数のカウントから外す」ための関数
  def eff_shift(s, d):
    return 0 if s in extra_work.get(d.strftime('%Y-%m-%d'), []) else shifts[(s, d)]

  # 4. 資格者数の確保
  req = rules['REQ_MIN']
  q_set = set(rules['QUALIFIED_STAFF'])

  for d in dates:
    w_name, d_str = weekday_name(d), d.strftime('%Y-%m-%d')
    req_tot = req['QUALIFIED_TOTAL'].get(w_name, 0)
    req_early = req['QUALIFIED_EARLY'].get(w_name)
    req_late = req['QUALIFIED_LATE'].get(w_name)

    if d not in jp_holidays and req_tot > 0:
      # 資格者全体数の判定
      all_q = [s for s in rules['STAFF'] if s in q_set]
      def_tot = model.NewIntVar(0, req_tot, f'def_q_tot_{d_str}')
      model.Add(sum(eff_shift(s, d) for s in all_q) + def_tot >= req_tot)
      penalty_terms.append(def_tot * 1000)

      # 早番の指定がある場合
      if req_early is not None:
        early_q = [s for s in rules['EARLY_STAFF'] if s in q_set]
        def_early = model.NewIntVar(0, req_early, f'def_q_early_{d_str}')
        model.Add(
            sum(eff_shift(s, d) for s in early_q) + def_early >= req_early
        )
        penalty_terms.append(def_early * 1000)

      # 遅番の指定がある場合
      if req_late is not None:
        late_q = [s for s in rules['LATE_STAFF'] if s in q_set]
        def_late = model.NewIntVar(0, req_late, f'def_q_late_{d_str}')
        model.Add(sum(eff_shift(s, d) for s in late_q) + def_late >= req_late)
        penalty_terms.append(def_late * 1000)

      deficiency_q_vars[d] = def_tot

  # 5. 一般スタッフ数の確保
  unqualified = rules['UNQUALIFIED_STAFF']
  for d in dates:
    w_name, d_str = weekday_name(d), d.strftime('%Y-%m-%d')
    req_unqual_num = req['UNQUALIFIED'].get(w_name, 0)

    if d not in jp_holidays and req_unqual_num > 0 and unqualified:
      def_unqual = model.NewIntVar(0, req_unqual_num, f'def_unqual_{d_str}')
      model.Add(
          sum(eff_shift(s, d) for s in unqualified) + def_unqual
          >= req_unqual_num
      )
      deficiency_unqual_vars[d] = def_unqual
      penalty_terms.append(def_unqual * 1000)

  if penalty_terms:
    model.Minimize(sum(penalty_terms))

  solver = cp_model.CpSolver()
  solver.parameters.max_time_in_seconds = 10.0
  status = solver.Solve(model)

  if status in [cp_model.OPTIMAL, cp_model.FEASIBLE]:
    res = {}
    for s in rules['STAFF']:
      row = []
      total_off = sum(1 for d in dates if solver.Value(shifts[(s, d)]) == 0)
      req_off = off_days_fulltime.get(s, 10)
      off_count = 0

      for d in dates:
        d_str = d.strftime('%Y-%m-%d')
        if solver.Value(shifts[(s, d)]) == 1:
          if s in extra_work.get(d_str, []):
            row.append('研')  # 研修日は文字「研」を表示
          else:
            w_name = weekday_name(d)
            req_early = req['QUALIFIED_EARLY'].get(w_name)
            req_late = req['QUALIFIED_LATE'].get(w_name)

            if req_early is not None or req_late is not None:
              is_e = s in rules['EARLY_STAFF']
              is_l = s in rules['LATE_STAFF']
              if is_e and is_l:
                row.append('出')
              elif is_e:
                row.append('早')
              elif is_l:
                row.append('遅')
              else:
                row.append('出')
            else:
              row.append('出')
        else:
          off_count += 1
          if (
              s in rules['FULL_TIME']
              and total_off > req_off
              and off_count > req_off
          ):
            row.append('有休')
          else:
            row.append('休')
      res[s] = row

    def_q_row, def_unqual_row = [], []
    for d in dates:
      tot_q_def = (
          solver.Value(deficiency_q_vars[d]) if d in deficiency_q_vars else 0
      )
      def_q_row.append(f'-{tot_q_def}人' if tot_q_def > 0 else '0')

      unqual_def = (
          solver.Value(deficiency_unqual_vars[d])
          if d in deficiency_unqual_vars
          else 0
      )
      def_unqual_row.append(f'-{unqual_def}人' if unqual_def > 0 else '0')

    res['⚠️資格者不足'] = def_q_row
    res['⚠️一般スタッフ不足'] = def_unqual_row

    return pd.DataFrame(
        res, index=[f'{d.month}/{d.day}({weekday_name(d)})' for d in dates]
    ).T
  return None


# ------------------------------------------------------------
# Streamlit UI
# ------------------------------------------------------------
st.set_page_config(page_title='自動シフト作成アプリ', layout='wide')
st.title('💊 自動シフト作成アプリ')

uploaded_file = st.file_uploader(
    '「Pythonシフト作成.xlsx」をアップロードしてください', type=['xlsx']
)

if uploaded_file is not None:
  if st.button('シフトを作成する', type='primary'):
    with st.spinner('最適なシフトを計算中...'):
      try:
        excel_bytes = uploaded_file.read()
        result_df = generate_shift_from_bytes(excel_bytes)

        if result_df is not None:
          st.success('✅ シフトが完成しました！')
          st.dataframe(result_df, use_container_width=True)

          output = io.BytesIO()
          with pd.ExcelWriter(output, engine='openpyxl') as writer:
            result_df.to_excel(writer, sheet_name='完成シフト')
          excel_data = output.getvalue()

          st.download_button(
              label='📥 完成シフト（Excel）をダウンロード',
              data=excel_data,
              file_name='完成シフト表.xlsx',
              mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          )
        else:
          st.error('❌ 条件が厳しすぎるためシフトを作成できませんでした。')
      except Exception as e:
        st.error(f'⚠️ エラーが発生しました: {e}')
