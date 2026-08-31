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


def load_fixed_rules_from_excel_bytes(excel_bytes, sheet_name='固定ルール'):
  # io.BytesIO でラップして pandas に渡すように修正
  excel_file = io.BytesIO(excel_bytes)
  df_raw = pd.read_excel(excel_file, sheet_name=sheet_name, header=None)

  req_min = {
      'WEEKDAY_EARLY': 2,
      'WEEKDAY_LATE': 2,
      'SATURDAY': 3,
      'SUNDAY': 2,
      'UNQUAL_MON': 1,
      'UNQUAL_TUE': 1,
      'UNQUAL_WED': 1,
      'UNQUAL_THU': 1,
      'UNQUAL_FRI': 1,
      'UNQUAL_SAT': 1,
      'UNQUAL_SUN': 1,
  }

  col_map = {'月': 3, '火': 4, '水': 5, '木': 6, '金': 7, '土': 8, '日': 9}

  for r in range(len(df_raw)):
    label = str(df_raw.iloc[r, 1]).strip() if pd.notna(df_raw.iloc[r, 1]) else ''
    if '平日早番' in label and pd.notna(df_raw.iloc[r, 3]):
      req_min['WEEKDAY_EARLY'] = int(df_raw.iloc[r, 3])
    elif '平日遅番' in label and pd.notna(df_raw.iloc[r, 3]):
      req_min['WEEKDAY_LATE'] = int(df_raw.iloc[r, 3])
    elif (
        '土曜' in label
        and 'うち' not in label
        and pd.notna(df_raw.iloc[r, col_map['土']])
    ):
      req_min['SATURDAY'] = int(df_raw.iloc[r, col_map['土']])
    elif (
        '日曜' in label
        and 'うち' not in label
        and pd.notna(df_raw.iloc[r, col_map['日']])
    ):
      req_min['SUNDAY'] = int(df_raw.iloc[r, col_map['日']])
    elif '一般スタッフ' in label:
      for w_key, col_i in col_map.items():
        if col_i < len(df_raw.columns) and pd.notna(df_raw.iloc[r, col_i]):
          key_name = f"UNQUAL_{'MON' if w_key=='月' else 'TUE' if w_key=='火' else 'WED' if w_key=='水' else 'THU' if w_key=='木' else 'FRI' if w_key=='金' else 'SAT' if w_key=='土' else 'SUN'}"
          req_min[key_name] = int(df_raw.iloc[r, col_i])

  header_row_idx = None
  for idx, row in df_raw.iterrows():
    if 'スタッフ一覧' in [str(v).strip() for v in row.values if pd.notna(v)]:
      header_row_idx = idx
      break

  excel_file.seek(0)
  df_staff = pd.read_excel(
      excel_file, sheet_name=sheet_name, skiprows=header_row_idx
  )
  df_staff.columns = [str(c).strip() for c in df_staff.columns]

  staff_col = [c for c in df_staff.columns if 'スタッフ' in c][0]
  type_col = [c for c in df_staff.columns if '区分' in c][0]
  job_col = [c for c in df_staff.columns if '職業' in c][0]
  time_col = [c for c in df_staff.columns if '時間帯' in c][0]
  days_col = [c for c in df_staff.columns if '出勤可能日' in c][0]
  max_c_col = [c for c in df_staff.columns if '最大連勤' in c][0]

  staff_list, full_time, part_time = [], [], []
  qualified_staff, unqualified_staff = [], []
  early_staff, late_staff, sat_staff, sun_staff = [], [], [], []
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

    t_band = str(row[time_col]).strip()
    if '早番' in t_band:
      early_staff.append(s_val)
    elif '遅番' in t_band:
      late_staff.append(s_val)

    w_days = [
        d.strip()
        for d in str(row[days_col]).replace('、', ',').split(',')
        if d.strip()
    ]
    fixed_holidays[s_val] = [d for d in WEEKDAYS_JP if d not in w_days]
    if '土' in w_days:
      sat_staff.append(s_val)
    if '日' in w_days:
      sun_staff.append(s_val)

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
      'SATURDAY_STAFF': sat_staff,
      'SUNDAY_STAFF': sun_staff,
      'FIXED_HOLIDAYS': fixed_holidays,
      'MAX_CONSECUTIVE': max_consecutive,
      'REQ_MIN': req_min,
  }


# ------------------------------------------------------------
# シフト生成処理
# ------------------------------------------------------------
def generate_shift_from_bytes(excel_bytes):
  rules = load_fixed_rules_from_excel_bytes(excel_bytes, sheet_name='固定ルール')

  # io.BytesIO でラップして pandas に渡すように修正
  excel_file = io.BytesIO(excel_bytes)
  df_cal = pd.read_excel(excel_file, sheet_name='カレンダー入力', header=None)

  holiday_requests, extra_work = {}, {}
  dates_list = []
  staff_cols = {
      '①': 5,
      '②': 6,
      '③': 7,
      '④': 8,
      '⑤': 9,
      '⑥': 10,
      '⑦': 11,
      '⑧': 12,
      '⑨': 13,
  }

  for r in range(4, len(df_cal)):
    d_val = df_cal.iloc[r, 3]
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
        if cell_val in ['公休', '休', '希望休', '×']:
          holiday_requests[d_str].append(s_name)
        elif cell_val in ['研', '研修']:
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
      else:
        if is_extra:
          model.Add(shifts[(s, d)] == 1)
        elif is_req_off or is_j_holiday or is_fixed_off:
          model.Add(shifts[(s, d)] == 0)

  off_days_fulltime = {s: 10 for s in rules['FULL_TIME']}
  for s in rules['FULL_TIME']:
    req_off = off_days_fulltime.get(s, 10)
    total_off = sum(1 - shifts[(s, d)] for d in dates)
    model.Add(total_off >= req_off)
    penalty_terms.append(total_off * 10)

  for s, max_c in rules['MAX_CONSECUTIVE'].items():
    for i in range(len(dates) - max_c):
      model.Add(
          sum(shifts[(s, dates[i + j])] for j in range(max_c + 1)) <= max_c
      )

  def eff_shift(s, d):
    return 0 if s in extra_work.get(d.strftime('%Y-%m-%d'), []) else shifts[(s, d)]

  req = rules['REQ_MIN']
  q_set = set(rules['QUALIFIED_STAFF'])

  for d in dates:
    w_name, d_str = weekday_name(d), d.strftime('%Y-%m-%d')
    if d not in jp_holidays:
      if w_name in ['月', '火', '水', '木', '金']:
        early_q = [
            s
            for s in rules['EARLY_STAFF']
            if s in q_set and s in rules['STAFF']
        ]
        def_early = model.NewIntVar(
            0, req['WEEKDAY_EARLY'], f'def_early_{d_str}'
        )
        model.Add(
            sum(eff_shift(s, d) for s in early_q) + def_early
            >= req['WEEKDAY_EARLY']
        )
        deficiency_q_vars[(d, 'early')] = def_early
        penalty_terms.append(def_early * 1000)

        late_q = [
            s for s in rules['LATE_STAFF'] if s in q_set and s in rules['STAFF']
        ]
        def_late = model.NewIntVar(0, req['WEEKDAY_LATE'], f'def_late_{d_str}')
        model.Add(
            sum(eff_shift(s, d) for s in late_q) + def_late
            >= req['WEEKDAY_LATE']
        )
        deficiency_q_vars[(d, 'late')] = def_late
        penalty_terms.append(def_late * 1000)

      elif w_name == '土':
        sat_q = [
            s
            for s in rules['SATURDAY_STAFF']
            if s in q_set and s in rules['STAFF']
        ]
        def_sat = model.NewIntVar(0, req['SATURDAY'], f'def_sat_{d_str}')
        model.Add(
            sum(eff_shift(s, d) for s in sat_q) + def_sat >= req['SATURDAY']
        )
        deficiency_q_vars[(d, 'sat')] = def_sat
        penalty_terms.append(def_sat * 1000)

      elif w_name == '日':
        sun_q = [
            s for s in rules['SUNDAY_STAFF'] if s in q_set and s in rules['STAFF']
        ]
        def_sun = model.NewIntVar(0, req['SUNDAY'], f'def_sun_{d_str}')
        model.Add(
            sum(eff_shift(s, d) for s in sun_q) + def_sun >= req['SUNDAY']
        )
        deficiency_q_vars[(d, 'sun')] = def_sun
        penalty_terms.append(def_sun * 1000)

  unqualified = rules['UNQUALIFIED_STAFF']
  w_map = {
      '月': 'MON',
      '火': 'TUE',
      '水': 'WED',
      '木': 'THU',
      '金': 'FRI',
      '土': 'SAT',
      '日': 'SUN',
  }

  for d in dates:
    w_name, d_str = weekday_name(d), d.strftime('%Y-%m-%d')
    req_key = f'UNQUAL_{w_map[w_name]}'
    req_unqual_num = req.get(req_key, 1)

    if d not in jp_holidays and unqualified:
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
            row.append('研')
          else:
            t = (
                '早'
                if s in rules['EARLY_STAFF']
                else ('遅' if s in rules['LATE_STAFF'] else '出')
            )
            row.append(t)
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
      q_defs = [
          solver.Value(var)
          for key, var in deficiency_q_vars.items()
          if key[0] == d
      ]
      tot_q_def = sum(q_defs) if q_defs else 0
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

          # Excelダウンロード用データの生成
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
