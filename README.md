# מחולל קבצי מרקטפלייס לזאפ (HDSALE)

סקריפט אחד (`generate.py`, Python בלבד, ללא התקנות) שרץ כל שעה ב-GitHub Actions:
קורא את גיליון "זאפ מרקטפלייס" ואת פיד קרוספרה הקיים, ומפרסם אתר סטטי ב-GitHub Pages:

- `index.html` רשימת קטגוריות (הקישור שנותנים לזאפ)
- `categories/<קטגוריה>.xml`
- `products/<מקט>.xml` (ProductInfo מורחב עם וריאציות)

מופיעים רק שורות עם פעיל=כן וכמות לפרסום גדולה מ-0.

## הפעלה
1. צור מאגר (repository) ב-GitHub והעלה את כל הקבצים.
2. Settings > Pages > Source: GitHub Actions.
3. שתף את הגיליון: כל מי שיש לו הקישור > צופה.
4. (אופציונלי) Settings > Secrets and variables > Actions > Variables: `BASE_URL` (כתובת סופית), `SHEET_ID`.
5. Actions > Zap marketplace feed > Run workflow. הדוח מופיע בסיכום ההרצה.

## בדיקות מקומיות
`python -m unittest discover -s tests`

חשוב: אל תחבר את זאפ לפני שכתובת ה-BASE_URL סופית.
