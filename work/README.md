# خط أنابيب استخراج نص الكتابين

محرّك تفريغ مخصّص للكتابين:

- `كتاب رتل مصمم ver3 - Part2.pdf`
- `كتاب رتل مصمم ver3 - Part3.pdf`

المشكلة: النص العربي في الكتابين مكتوب بخطوط CID (Identity-H) بترتيب مرئي
يسار-إلى-يمين، ولا توجد خرائط ToUnicode صحيحة لدى مكتبات الاستخراج الجاهزة،
وخرائط Word المضمّنة في خطّ SakkalMajalla العادي خاطئة جزئياً.

## المكوّنات

| الملف | الوظيفة |
| --- | --- |
| `pdf_decoder.py` | محلّل تيارات المحتوى (Tj/TJ/Tm/Td/TD) مع تتبّع مواضع الرموز |
| `cmap_parser.py` | محلّل خرائط ToUnicode المضمّنة في PDF |
| `font_fixer.py` | تصحيح خرائط الرموز بمطابقة أشكال الحروف مع الخطوط الأصلية الكاملة |
| `reverse_resolver.py` | حلّ الرموز بعكس جداول GSUB وبمطابقة الأشكال (احتياطي) |
| `decode_driver.py` | السائق: تجميع الحروف في عناقيد وأسطر بترتيب منطقي RTL |

## الخطوط الخارجية (لا تُلحق بالمستودع)

تُنزَّل في `work/fonts/`:

- `majalla.ttf` / `majallab.ttf` — Sakkal Majalla (Aya-Ibrahim261/majalla-font)
- `UthmanicHafs1_Ver09.otf` — خط KFGQPC القرآني (nuqayah/qpc-fonts)

## التشغيل

```bash
python work/decode_driver.py "cmd/كتاب رتل مصمم ver3 - Part2.pdf" work/part2_decoded.txt
python work/decode_driver.py "cmd/كتاب رتل مصمم ver3 - Part3.pdf" work/part3_decoded.txt
```

## الحالة

- `part2_decoded.txt` — تفريغ وسيط للجزء الثاني (قابل للقراءة؛ جارٍ استكمال
  المراجعة اللغوية وإعداد تقرير التصحيحات).
