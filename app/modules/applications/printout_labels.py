"""Every word the two application printouts print, in the five document
languages (stage 16, ruling R5). Wording only — no logic.

`uz_cyrl` is the Agency's final blank of 2026-09-24 verbatim
(`docs/tz/forms/2026-09-24-ariza-va-rad-etish-blankalari.md`); `uz_latn` is its
transliteration; `ru`, `kaa` and `en` are our translations. A correction is a
code change (ruling R10).

`LETTER_LABELS` / `NOTICE_LABELS` fill the layouts' `{{ t_* }}` placeholders;
the `*_FORMAT` strings are phrases the snapshot builder formats in code,
because they carry data inside them.
"""

LETTER_LABELS: dict[str, dict[str, str]] = {
    "uz_latn": {
        "t_title": "OʻRMONDAN FOYDALANISH UCHUN ARIZA XATI",
        "t_number": "Ariza raqami",
        "t_date": "Ariza sanasi",
        "t_applicant": "Ariza beruvchi",
        "t_address": "Yashash manzili",
        "t_contact": "Aloqa",
        "t_heading": "ARIZA",
        "t_request_1": "Men, ",
        "t_request_2": (
            ", davlat oʻrmon fondi uchastkasida quyida koʻrsatilgan foydalanish turi boʻyicha "
            "ruxsat berilishini soʻrayman."
        ),
        "t_activity": "Foydalanish turi",
        "t_territory": "Hudud",
        "t_plot": "Uchastka maʼlumoti",
        "t_coordinates": "Koordinatalar",
        "t_period": "Muddat",
        "t_quantity": "Miqdor va birlik",
        "t_purpose": "Maqsad",
        "t_undertaking": (
            "Belgilangan toʻlovni hisob-kitob asosida toʻlash, oʻrmon va yaylovdan foydalanish, "
            "yongʻin xavfsizligi, sanitariya hamda tabiatni muhofaza qilish talablariga rioya "
            "etish majburiyatini qabul qilaman. Tizimga kiritilgan maʼlumotlar va ilova qilingan "
            "hujjatlarning haqqoniyligi uchun javobgarman."
        ),
        "t_attachments": "Ilovalar",
        "t_confirmation": "Elektron tasdiq",
    },
    "uz_cyrl": {
        "t_title": "ЎРМОНДАН ФОЙДАЛАНИШ УЧУН АРИЗА ХАТИ",
        "t_number": "Ариза рақами",
        "t_date": "Ариза санаси",
        "t_applicant": "Ариза берувчи",
        "t_address": "Яшаш манзили",
        "t_contact": "Алоқа",
        "t_heading": "АРИЗА",
        "t_request_1": "Мен, ",
        "t_request_2": (
            ", давлат ўрмон фонди участкасида қуйида кўрсатилган фойдаланиш тури бўйича рухсат "
            "берилишини сўрайман."
        ),
        "t_activity": "Фойдаланиш тури",
        "t_territory": "Ҳудуд",
        "t_plot": "Участка маълумоти",
        "t_coordinates": "Координаталар",
        "t_period": "Муддат",
        "t_quantity": "Миқдор ва бирлик",
        "t_purpose": "Мақсад",
        "t_undertaking": (
            "Белгиланган тўловни ҳисоб-китоб асосида тўлаш, ўрмон ва яйловдан фойдаланиш, ёнғин "
            "хавфсизлиги, санитария ҳамда табиатни муҳофаза қилиш талабларига риоя этиш "
            "мажбуриятини қабул қиламан. Тизимга киритилган маълумотлар ва илова қилинган "
            "ҳужжатларнинг ҳаққонийлиги учун жавобгарман."
        ),
        "t_attachments": "Иловалар",
        "t_confirmation": "Электрон тасдиқ",
    },
    "ru": {
        "t_title": "ЗАЯВЛЕНИЕ НА ПОЛЬЗОВАНИЕ ЛЕСОМ",
        "t_number": "Номер заявления",
        "t_date": "Дата заявления",
        "t_applicant": "Заявитель",
        "t_address": "Адрес проживания",
        "t_contact": "Контакты",
        "t_heading": "ЗАЯВЛЕНИЕ",
        "t_request_1": "Я, ",
        "t_request_2": (
            ", прошу выдать разрешение на пользование участком государственного лесного фонда по "
            "указанному ниже виду пользования."
        ),
        "t_activity": "Вид пользования",
        "t_territory": "Территория",
        "t_plot": "Сведения об участке",
        "t_coordinates": "Координаты",
        "t_period": "Срок",
        "t_quantity": "Количество и единица",
        "t_purpose": "Цель",
        "t_undertaking": (
            "Обязуюсь уплатить установленный платёж на основании расчёта, соблюдать требования "
            "пользования лесом и пастбищами, пожарной безопасности, санитарии и охраны природы. "
            "Несу ответственность за достоверность сведений, внесённых в систему, и приложенных "
            "документов."
        ),
        "t_attachments": "Приложения",
        "t_confirmation": "Электронное подтверждение",
    },
    "kaa": {
        "t_title": "TOǴAYDAN PAYDALANÍW USHÍN ARZA XATÍ",
        "t_number": "Arza nomeri",
        "t_date": "Arza sánesi",
        "t_applicant": "Arza beriwshi",
        "t_address": "Jasaw mánzili",
        "t_contact": "Baylanıs",
        "t_heading": "ARZA",
        "t_request_1": "Men, ",
        "t_request_2": (
            ", mámleketlik toǵay fondı ushastkasınan tómende kórsetilgen paydalanıw túri boyınsha "
            "ruxsat beriliwin soraymán."
        ),
        "t_activity": "Paydalanıw túri",
        "t_territory": "Aymaq",
        "t_plot": "Ushastka maǵlıwmatı",
        "t_coordinates": "Koordinatalar",
        "t_period": "Múddet",
        "t_quantity": "Muǵdar hám birlik",
        "t_purpose": "Maqset",
        "t_undertaking": (
            "Belgilengen tólemdi esap-kitap tiykarında tólew, toǵay hám jaylawdan paydalanıw, órt "
            "qáwipsizligi, sanitariya hám tábiyattı qorǵaw talaplarına ámel etiw minnetlemesin "
            "qabıl etemen. Sistemaǵa kiritilgen maǵlıwmatlar hám qosımsha etilgen hújjetlerdiń "
            "durıslıǵı ushın juwapkerman."
        ),
        "t_attachments": "Qosımshalar",
        "t_confirmation": "Elektron tastıyıq",
    },
    "en": {
        "t_title": "APPLICATION FOR THE USE OF FOREST LAND",
        "t_number": "Application No.",
        "t_date": "Application date",
        "t_applicant": "Applicant",
        "t_address": "Home address",
        "t_contact": "Contact",
        "t_heading": "APPLICATION",
        "t_request_1": "I, ",
        "t_request_2": (
            ", request a permit to use a plot of the state forest fund for the type of use stated "
            "below."
        ),
        "t_activity": "Type of use",
        "t_territory": "Territory",
        "t_plot": "Plot",
        "t_coordinates": "Coordinates",
        "t_period": "Period",
        "t_quantity": "Quantity and unit",
        "t_purpose": "Purpose",
        "t_undertaking": (
            "I undertake to pay the fee set by the calculation and to observe the requirements of "
            "forest and pasture use, fire safety, sanitation and nature protection. I am "
            "responsible for the accuracy of the data entered into the system and of the attached "
            "documents."
        ),
        "t_attachments": "Attachments",
        "t_confirmation": "Electronic confirmation",
    },
}

NOTICE_LABELS: dict[str, dict[str, str]] = {
    "uz_latn": {
        "t_title": "ARIZANI RAD ETISH TOʻGʻRISIDA XABARNOMA",
        "t_notice_number": "Xabarnoma raqami",
        "t_decision_date": "Qaror sanasi",
        "t_application_number": "Ariza raqami",
        "t_reviewer": "Koʻrib chiquvchi",
        "t_result": "Natija",
        "t_result_value": "RAD ETILDI",
        "t_ground_category": "Rad etish toifasi",
        "t_ground_fact": "Aniqlangan holat",
        "t_ground_legal": "Huquqiy asos",
        "t_ground_evidence": "Dalil va manba",
        "t_ground_remedy": "Bartaraf etish tartibi",
        "t_reapply": "Qayta murojaat",
        "t_appeal": "Shikoyat qilish",
        "t_closing": (
            "Ushbu xabarnoma ariza boʻyicha yakuniy qarorning asoslari bilan tanishish imkonini "
            "beradi. Kamchiliklar bartaraf etilgandan keyin, agar qonunchilikda boshqacha cheklov "
            "belgilanmagan boʻlsa, axborot tizimi orqali qayta ariza berish mumkin."
        ),
        "t_moderator": "Moderator",
        "t_confirmation": "Elektron tasdiq",
    },
    "uz_cyrl": {
        "t_title": "АРИЗАНИ РАД ЭТИШ ТЎҒРИСИДА ХАБАРНОМА",
        "t_notice_number": "Хабарнома рақами",
        "t_decision_date": "Қарор санаси",
        "t_application_number": "Ариза рақами",
        "t_reviewer": "Кўриб чиқувчи",
        "t_result": "Натижа",
        "t_result_value": "РАД ЭТИЛДИ",
        "t_ground_category": "Рад этиш тоифаси",
        "t_ground_fact": "Аниқланган ҳолат",
        "t_ground_legal": "Ҳуқуқий асос",
        "t_ground_evidence": "Далил ва манба",
        "t_ground_remedy": "Бартараф этиш тартиби",
        "t_reapply": "Қайта мурожаат",
        "t_appeal": "Шикоят қилиш",
        "t_closing": (
            "Ушбу хабарнома ариза бўйича якуний қарорнинг асослари билан танишиш имконини беради. "
            "Камчиликлар бартараф этилгандан кейин, агар қонунчиликда бошқача чеклов белгиланмаган "
            "бўлса, ахборот тизими орқали қайта ариза бериш мумкин."
        ),
        "t_moderator": "Модератор",
        "t_confirmation": "Электрон тасдиқ",
    },
    "ru": {
        "t_title": "УВЕДОМЛЕНИЕ ОБ ОТКАЗЕ В УДОВЛЕТВОРЕНИИ ЗАЯВЛЕНИЯ",
        "t_notice_number": "Номер уведомления",
        "t_decision_date": "Дата решения",
        "t_application_number": "Номер заявления",
        "t_reviewer": "Рассмотрел",
        "t_result": "Результат",
        "t_result_value": "ОТКАЗАНО",
        "t_ground_category": "Категория отказа",
        "t_ground_fact": "Установленное обстоятельство",
        "t_ground_legal": "Правовое основание",
        "t_ground_evidence": "Доказательство и источник",
        "t_ground_remedy": "Порядок устранения",
        "t_reapply": "Повторное обращение",
        "t_appeal": "Обжалование",
        "t_closing": (
            "Настоящее уведомление позволяет ознакомиться с основаниями окончательного решения по "
            "заявлению. После устранения недостатков, если законодательством не установлено иное "
            "ограничение, можно повторно подать заявление через информационную систему."
        ),
        "t_moderator": "Модератор",
        "t_confirmation": "Электронное подтверждение",
    },
    "kaa": {
        "t_title": "ARZANÍ BIYKAR ETIW HAQQÍNDA XABARNAMA",
        "t_notice_number": "Xabarnama nomeri",
        "t_decision_date": "Sheshim sánesi",
        "t_application_number": "Arza nomeri",
        "t_reviewer": "Kórip shıǵıwshı",
        "t_result": "Nátiyje",
        "t_result_value": "BIYKAR ETILDI",
        "t_ground_category": "Biykar etiw túri",
        "t_ground_fact": "Anıqlanǵan jaǵday",
        "t_ground_legal": "Huqıqıy tiykar",
        "t_ground_evidence": "Dálil hám derek",
        "t_ground_remedy": "Saplastırıw tártibi",
        "t_reapply": "Qayta múrájat",
        "t_appeal": "Shaǵım etiw",
        "t_closing": (
            "Usı xabarnama arza boyınsha aqırǵı sheshimniń tiykarları menen tanısıw imkaniyatın "
            "beredi. Kemshilikler saplastırılǵannan keyin, eger nızamshılıqta basqasha sheklew "
            "belgilenbegen bolsa, informaciyalıq sistema arqalı qayta arza beriw múmkin."
        ),
        "t_moderator": "Moderator",
        "t_confirmation": "Elektron tastıyıq",
    },
    "en": {
        "t_title": "NOTICE OF REJECTION OF AN APPLICATION",
        "t_notice_number": "Notice No.",
        "t_decision_date": "Decision date",
        "t_application_number": "Application No.",
        "t_reviewer": "Reviewed by",
        "t_result": "Result",
        "t_result_value": "REJECTED",
        "t_ground_category": "Rejection category",
        "t_ground_fact": "Fact established",
        "t_ground_legal": "Legal basis",
        "t_ground_evidence": "Evidence and source",
        "t_ground_remedy": "How to remedy",
        "t_reapply": "Re-applying",
        "t_appeal": "Appeal",
        "t_closing": (
            "This notice sets out the grounds of the final decision on the application. Once the "
            "shortcomings are remedied, and unless the law provides otherwise, a new application "
            "may be filed through the information system."
        ),
        "t_moderator": "Moderator",
        "t_confirmation": "Electronic confirmation",
    },
}

# Phrases with data inside, formatted by `printouts.py`.
LETTER_ADDRESSEE_FORMAT = {
    "uz_latn": "{org} rahbari {name}ga",
    "uz_cyrl": "{org} раҳбари {name}га",
    "ru": "Руководителю {org} {name}",
    "kaa": "{org} basshısı {name}ǵa",
    "en": "To the head of {org}, {name}",
}
LETTER_ADDRESSEE_NO_NAME_FORMAT = {
    "uz_latn": "{org} rahbariga",
    "uz_cyrl": "{org} раҳбарига",
    "ru": "Руководителю {org}",
    "kaa": "{org} basshısına",
    "en": "To the head of {org}",
}
NOTICE_ADDRESSEE_FORMAT = {
    "uz_latn": "{name}ga",
    "uz_cyrl": "{name}га",
    "ru": "{name}",
    "kaa": "{name}ǵa",
    "en": "To {name}",
}
PERSONAL_CABINET = {
    "uz_latn": "Shaxsiy kabinet",
    "uz_cyrl": "Шахсий кабинет",
    "ru": "Личный кабинет",
    "kaa": "Jeke kabinet",
    "en": "Personal account",
}
PERIOD_FORMAT = {
    "uz_latn": "{start} dan {end} gacha",
    "uz_cyrl": "{start} дан {end} гача",
    "ru": "с {start} по {end}",
    "kaa": "{start} dan {end} ge shekem",
    "en": "{start} to {end}",
}
PLOT_FORMAT = {
    "uz_latn": "Kontur № {contour}, maydon {area} ga",
    "uz_cyrl": "Контур № {contour}, майдон {area} га",
    "ru": "Контур № {contour}, площадь {area} га",
    "kaa": "Kontur № {contour}, maydanı {area} ga",
    "en": "Contour No. {contour}, area {area} ha",
}
NOTICE_BODY_FORMAT = {
    "uz_latn": (
        "Sizning {date} kuni berilgan {number}-son arizangiz va unga ilova qilingan hujjatlar "
        "koʻrib chiqildi. Koʻrib chiqish natijasiga koʻra ruxsatnoma berish quyidagi asos(lar) "
        "boʻyicha rad etildi."
    ),
    "uz_cyrl": (
        "Сизнинг {date} куни берилган {number}-сон аризангиз ва унга илова қилинган ҳужжатлар "
        "кўриб чиқилди. Кўриб чиқиш натижасига кўра рухсатнома бериш қуйидаги асос(лар) бўйича рад "
        "этилди."
    ),
    "ru": (
        "Ваше заявление № {number} от {date} и приложенные к нему документы рассмотрены. По "
        "результатам рассмотрения в выдаче разрешения отказано по следующим основаниям."
    ),
    "kaa": (
        "Sizdiń {date} kúni berilgen {number}-sanlı arzańız hám oǵan qosımsha etilgen hújjetler "
        "kórip shıǵıldı. Kórip shıǵıw nátiyjesine kóre ruxsatnama beriw tómendegi tiykar(lar) "
        "boyınsha biykar etildi."
    ),
    "en": (
        "Your application No. {number} of {date} and the documents attached to it have been "
        "reviewed. The permit has been refused on the following ground(s)."
    ),
}
SIGNATURE_ERI_FORMAT = {
    "uz_latn": "ERI, sertifikat № {serial}",
    "uz_cyrl": "ЭРИ, сертификат № {serial}",
    "ru": "ЭЦП, сертификат № {serial}",
    "kaa": "ERI, sertifikat № {serial}",
    "en": "Digital signature (ERI), certificate No. {serial}",
}
SIGNATURE_SIMPLE_FORMAT = {
    "uz_latn": "OneID orqali tasdiqlangan, ID {ref}",
    "uz_cyrl": "OneID орқали тасдиқланган, ID {ref}",
    "ru": "Подтверждено через OneID, ID {ref}",
    "kaa": "OneID arqalı tastıyıqlanǵan, ID {ref}",
    "en": "Confirmed via OneID, ID {ref}",
}
UNIT_LABELS = {
    "uz_latn": {
        "head": "bosh",
        "ton": "tonna",
        "hive": "quti",
        "ha": "ga",
        "person": "kishi",
        "unit": "dona",
    },
    "uz_cyrl": {
        "head": "бош",
        "ton": "тонна",
        "hive": "қути",
        "ha": "га",
        "person": "киши",
        "unit": "дона",
    },
    "ru": {
        "head": "голов",
        "ton": "т",
        "hive": "ульев",
        "ha": "га",
        "person": "чел.",
        "unit": "шт.",
    },
    "kaa": {
        "head": "bas",
        "ton": "tonna",
        "hive": "quti",
        "ha": "ga",
        "person": "adam",
        "unit": "dana",
    },
    "en": {
        "head": "head",
        "ton": "t",
        "hive": "hives",
        "ha": "ha",
        "person": "persons",
        "unit": "units",
    },
}
DEADWOOD_PRODUCT_LABELS = {
    "uz_latn": {
        "firewood": "oʻtin",
        "branches": "shox-shabba",
        "both": "oʻtin va shox-shabba",
    },
    "uz_cyrl": {
        "firewood": "ўтин",
        "branches": "шох-шабба",
        "both": "ўтин ва шох-шабба",
    },
    "ru": {
        "firewood": "дрова",
        "branches": "хворост",
        "both": "дрова и хворост",
    },
    "kaa": {
        "firewood": "otın",
        "branches": "shaq-shabaq",
        "both": "otın hám shaq-shabaq",
    },
    "en": {
        "firewood": "firewood",
        "branches": "branches",
        "both": "firewood and branches",
    },
}
RECREATION_PURPOSE_LABELS = {
    "uz_latn": {
        "cultural_educational": "madaniy-maʼrifiy",
        "upbringing": "tarbiyaviy",
        "health": "sogʻlomlashtirish",
        "recreational": "rekreatsion",
        "aesthetic": "estetik",
    },
    "uz_cyrl": {
        "cultural_educational": "маданий-маърифий",
        "upbringing": "тарбиявий",
        "health": "соғломлаштириш",
        "recreational": "рекреацион",
        "aesthetic": "эстетик",
    },
    "ru": {
        "cultural_educational": "культурно-просветительская",
        "upbringing": "воспитательная",
        "health": "оздоровительная",
        "recreational": "рекреационная",
        "aesthetic": "эстетическая",
    },
    "kaa": {
        "cultural_educational": "mádeniy-aǵartıwshılıq",
        "upbringing": "tárbiyalıq",
        "health": "salamatlandırıw",
        "recreational": "rekreaciyalıq",
        "aesthetic": "estetikalıq",
    },
    "en": {
        "cultural_educational": "cultural and educational",
        "upbringing": "upbringing",
        "health": "health",
        "recreational": "recreational",
        "aesthetic": "aesthetic",
    },
}
PURPOSE_DEADWOOD_FORMAT = {
    "uz_latn": "{product}; olib chiqish muddati: {deadline}",
    "uz_cyrl": "{product}; олиб чиқиш муддати: {deadline}",
    "ru": "{product}; срок вывоза: {deadline}",
    "kaa": "{product}; alıp shıǵıw múddeti: {deadline}",
    "en": "{product}; removal deadline: {deadline}",
}
PURPOSE_RECREATION_FORMAT = {
    "uz_latn": "{purpose}; tadbir vaqti: {event_at}",
    "uz_cyrl": "{purpose}; тадбир вақти: {event_at}",
    "ru": "{purpose}; время мероприятия: {event_at}",
    "kaa": "{purpose}; ilaje waqtı: {event_at}",
    "en": "{purpose}; event time: {event_at}",
}
NOT_STATED = "—"
