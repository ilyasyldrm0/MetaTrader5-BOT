# MetaTrader5-BOT

[![CI](https://github.com/ilyasyldrm0/MetaTrader5-BOT/actions/workflows/ci.yml/badge.svg)](https://github.com/ilyasyldrm0/MetaTrader5-BOT/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE.md)

MetaTrader 5 için Python ile yazılmış RSI + hareketli ortalama alım-satım botu.

Backtest motoru, kâğıt üzerinde işlem modu, broker sözleşme detaylarını kontrol
eden bir `doctor` komutu ve MetaTrader kurulu olmadan çalışan bir test paketi
içerir. **`--live` vermediğiniz sürece işlem açmaz.**

*[English README](README.md)*

---

## Önceki sürümü kullandıysanız, bunu okuyun

2023'teki tek dosyalık `main.py` emir gönderemiyordu. "Kötü işlem yapıyordu"
değil — hiç emir gönderemiyordu:

```python
trade_profit = 50                       # README "pip cinsinden" diyordu
take_profit  = last_close + trade_profit
```

EURUSD 1.08 iken bu, **51.08**'de bir kâr al ve **-23.92**'de, yani negatif bir
fiyatta zarar durdur istiyor. Her emir `INVALID_STOPS` ile geri geliyordu.
Kapatma isteği de eksikti, dolayısıyla hiçbir pozisyon kapatılamıyordu.

Geri dönen bir kullanıcıyı şaşırtacak üç değişiklik:

| | Eskiden | Şimdi |
|---|---|---|
| **Stop mesafeleri** | fiyata ekleniyordu, 50 = 50.0 demekti | 50 = 50 pip, README'nin baştan beri iddia ettiği gibi |
| **Başlatınca** | anında işlem açardı | simüle eder; emir göndermek için `--live` şart |
| **Giriş noktası** | `python main.py` | `python main.py run` (`main.py` çalışmaya devam ediyor, `--help` gerisini listeler) |

Varsayılan sayılar değişmedi. Değişen, *ne anlama geldikleri* — artık
belgelenen şeyi ifade ediyorlar.

---

## Hızlı başlangıç

```bash
git clone https://github.com/ilyasyldrm0/MetaTrader5-BOT
cd MetaTrader5-BOT
pip install -e .
```

Önce stratejiyi geçmiş veri üzerinde ölçün — bunun için MetaTrader gerekmez,
her işletim sisteminde çalışır:

```bash
python main.py backtest --csv tests/data/eurusd_m15_sample.csv
```

Ardından, terminal açıkken Windows'ta:

```bash
python main.py doctor       # her şeye erişilebiliyor ve doğru yapılandırılmış mı?
python main.py run          # kuru çalışma: gerçek fiyatlar, emir gönderilmez
python main.py run --live   # gerçek emirler. Önce bu dosyanın kalanını okuyun.
```

---

## Gereksinimler

* **Python 3.11+**
* `pandas` ve `numpy` — paketle birlikte kurulur
* Canlı işlem için **MetaTrader 5 (yalnızca Windows)**: `pip install MetaTrader5`

`MetaTrader5` paketi yalnızca Windows'ta çalışır. Bu kısıt tasarımı belirledi:
broker arayüzünün üstündeki her şey saf Python, dolayısıyla **backtest, kâğıt
üzerinde işlem ve testlerin tamamı Linux ve macOS'ta çalışır.** Yalnızca
`run --live` Windows gerektirir. Linux'ta terminali Wine altında da
çalıştırabilir veya dışa aktarılmış veriyle `run --replay` kullanabilirsiniz.

---

## Komutlar

### `doctor` — işlem açmadan önce kontrol

Yapılandırmanızı doğrular, bağlanır ve broker'ınızın hesabınız ve sembolünüz
hakkında gerçekte ne söylediğini raporlar: spread, pip büyüklüğü, lot adımı,
minimum stop mesafesi ve emir doldurma politikası. Sonunda, backtest'lerin
*sizin* hesabınıza göre pozisyon büyüklüğü hesaplaması için config'inize
yapıştırabileceğiniz bir `[symbol]` bloğu basar.

```bash
python main.py doctor
python main.py --symbol GBPUSD doctor
```

### `backtest` — bağlanmadan önce ölç

```bash
python main.py backtest --csv data/EURUSD_M15.csv
python main.py backtest --csv data/EURUSD_M15.csv --trend-filter off --commission 7
python main.py backtest --csv data/EURUSD_M15.csv --journal trades.csv
```

İki CSV düzeni de biçim değiştirmeden okunur: düz
`time,open,high,low,close,volume` dosyası ve MetaTrader'ın *Araçlar → Geçmiş
Merkezi → Dışa Aktar* ile ürettiği sekmeyle ayrılmış dosya.

Backtest, canlı bir oturumla **aynı motoru ve aynı strateji nesnesini**
çalıştırır; terminalin yerini simüle edilmiş bir broker alır. Gerçeğinden
sapabilecek ikinci bir simülasyon döngüsü yoktur.

Ayrıca sizi kayırmaz:

* maliyetler her çalıştırmada yazılır, varsayılan olarak 1 pip spread kesilir
* 30'dan az kapanmış işlem uyarı basar — o bir örneklem değildir
* hem stop'a hem hedefe dokunan bir bar **zarar** sayılır; barlar fiyatların
  hangi sırayla ziyaret edildiği hakkında hiçbir şey söylemez
* bir seviyeyi boşlukla geçen bar, seviyeden değil açılıştan doldurulur
* veri bittiğinde hâlâ açık olan pozisyon, uydurma bir fiyattan kapatılmaz,
  hesap dışı bırakılır

### `run` — işlem yap, ya da yapıyormuş gibi

```bash
python main.py run                       # kuru çalışma (varsayılan)
python main.py run --live                # gerçek emirler
python main.py run --replay data.csv     # motoru dosyadan sür, tam hızda
python main.py run --live --config config.toml
```

Kuru çalışma gerçek fiyatları, spread'leri ve sözleşme detaylarını kullanır ve
göndereceği her emri günlüğe yazar. Terminale hiçbir şey ulaşmaz.

---

## Strateji, dürüstçe

Orijinal koşullar, varsayılan olarak korundu:

```
RSI(14) <= 30  VE  close > SMA(12)   ->  AL
RSI(14) >= 70  VE  close < SMA(12)   ->  SAT
```

Dikkatle okunduğunda bu, *trend hâlâ yukarı bakarken dipten al* demek — makul
bir fikir. Ama iki yarısı birbirini çekiştiriyor. 15 dakikalık grafikte
RSI(14)'ü 30'un altına indiren şey, fiyatı neredeyse her zaman 12 periyotluk
ortalamanın da altına sürüklemiştir. Yani ikisini aynı anda istemek, pek
oluşmayan bir durumu istemektir.

Ne kadar nadir? Pakete dahil 4.000 barlık örnek üzerinde:

| `trend_filter` | Sinyal | 1000 barda |
|---|---|---|
| `aligned` (orijinal) | **0** | 0.0 |
| `contrarian` | 413 | 105.1 |
| `off` | 413 | 105.1 |

Sıfır. Ve dikkat edin: `contrarian` ile `off` *birebir aynı* — hareketli
ortalamayı devre dışı bırakmak hiçbir şeyi değiştirmiyor, çünkü aşırı satım
gören her bar zaten ortalamanın altındaymış. Bu, aynı gerçeğin öbür yüzü.

Bu bir hata raporu değil. Bot tam olarak yapılandırıldığı şeyi yapıyor ve koşul
imkânsız değil, sadece nadir: uzun bir düşüş, ortalamanın yetişmesine yetecek
bir duraklama, ardından ılımlı bir toparlanma iki yarıyı aynı anda sağlar. Tam
bu şekli kuran bir test var.

Asıl mesele şu: artık yapılandırmayı izlenim yerine bir rakamla
tartışabilirsiniz. Her seferinde tek şeyi değiştirin:

| Ayar | Etkisi |
|---|---|
| `trend_filter = "contrarian"` | klasik ortalamaya dönüş: aşırı satımı, fiyat ortalamanın *altında olduğu için* al |
| `trend_filter = "off"` | yalnızca RSI eşikleriyle işlem yap |
| `oversold` / `overbought` | 35/65, 30/70'ten belirgin şekilde daha sık tetiklenir |
| `require_cross = true` | yalnızca eşiğin geçildiği barda sinyal üret |

> Pakete dahil `tests/data/eurusd_m15_sample.csv` **deterministik bir rastgele
> yürüyüştür, piyasa verisi değildir.** Trendi, seans yapısı ve haber etkisi
> yoktur; makineyi çalıştırır, ama bir stratejinin para kazanıp kazanmadığı
> hakkında hiçbir şey söylemez. Herhangi bir sonuç çıkarmadan önce kendi
> geçmiş verinizle backtest yapın. Yukarıdaki sinyal sıklığı bulgusu yapısaldır
> — RSI ile hareketli ortalamanın ilişkisinden gelir — ama o dosyadaki kâr
> rakamları anlamsızdır.

---

## Yapılandırma

`config.example.toml` dosyasını `config.toml` olarak kopyalayıp düzenleyin. Her
anahtarın bir varsayılanı var, umursamadığınızı silebilirsiniz. Bilinmeyen
anahtarlar yok sayılmaz, **reddedilir** — sessizce varsayılanı yerinde bırakan
bir yazım hatası, en pahalıya mal olan config hatasıdır.

```toml
[trading]
symbol    = "EURUSD"
timeframe = "M15"

[strategy]
trend_filter = "aligned"     # aligned | contrarian | off

[risk]
sl_pips      = 25.0          # pip. Fiyat değil.
tp_pips      = 50.0
sizing       = "risk"        # her işlemi, stop'ta risk_percent kaybedecek şekilde boyutla
risk_percent = 1.0
max_spread_pips    = 3.0
max_trades_per_day = 10
```

Kimlik bilgileri dosyaya değil ortam değişkenlerine — `.env.example`'a bakın:

```bash
export MT5_LOGIN=12345678
export MT5_PASSWORD=...
export MT5_SERVER=YourBroker-Demo
```

Hiçbiri ayarlanmazsa bot, çalışan terminalin zaten giriş yapmış olduğu hesaba
bağlanır.

### Pozisyon büyüklüğü

`sizing = "risk"`, her işlemi stop'a takılması hâlinde bakiyenin
`risk_percent` kadarını kaybedecek şekilde boyutlar. Bunu sembolün kendi tick
ekonomisi üzerinden yapar; sabit "pip başına 10 dolar" varsayımıyla değil (o
varsayım JPY paritelerinde yanlış, USD olmayan hesaplarda yanlış ve her CFD'de
yanlıştır).

10.000 $ bakiyede %1 risk ve 25 pip stop → 0.40 lot. O stop dolduğunda zarar
tam 100 $ olur — istenen %1'in ta kendisi. Her seferinde `fixed_lot`
göndermek için `sizing = "fixed"` yapın.

---

## Başka neler düzeltildi

Bu dosyanın başındaki stop mesafesi hatasının ötesinde:

* **RSI, Wilder'ın RSI'si değildi.** Kazanç ve kayıpları düz bir hareketli
  ortalamayla topluyordu, dolayısıyla değerler karşılaştırabileceğiniz her
  grafikten sapıyordu. Üst üste yükselen barlarda sıfır ortalama kayba bölüyor,
  yatay piyasada `0/0` üretiyordu — sonuç `NaN`, ve `NaN` her eşik
  karşılaştırmasında `False` döner; yani bot "trend yok" demek yerine sessizce
  susuyordu.
* **14 periyotluk RSI için 26 bar** geçmiş isteniyordu. Bu, tohumdan yalnızca
  on iki bar sonrası — tohumun ağırlığın hâlâ yaklaşık %41'ini taşıdığı nokta.
  Artık beş periyot alınıyor.
* **Sinyaller oluşmakta olan mumdan geliyordu.** 0. bar konumundan okuyup 10
  saniyede bir yoklamak, henüz oturmamış bir sayı üzerinden işlem yapmak için
  doksan fırsat veriyordu. Artık her *kapanmış* barda bir kez değerlendiriliyor.
* **Emirler son bar kapanışından fiyatlanıyordu** — on beş dakikaya kadar bayat
  — kayma toleransı olmadan ve FOK gerektiren broker'ların doğrudan reddettiği
  sabit IOC doldurma politikasıyla.
* **`order_send` `None` dönebilir.** Üzerinden `.retcode` okumak
  `AttributeError` fırlatıyor ve süreci açık pozisyonla öldürüyordu.
* **Pozisyon durumu iki değişkende tutuluyordu**; kapatmadan sonra hiç
  temizlenmiyor ve stop'un dolduğunu fark edemiyordu. Bot, artık var olmayan
  bir pozisyonu tuttuğuna inanmaya devam ediyordu. Durum artık her döngüde
  broker'dan okunuyor.
* **Başarısız bir kapatma yine de ters pozisyonu açıyordu**; hesap hedge'li ve
  çift marjinli kalıyordu. Kapatma artık iki kez doğrulanıyor — emir sonucundan
  ve açık pozisyonlar yeniden okunarak.
* **Hiç hata yakalama yoktu.** Tek bir istisna, süreci piyasada parayla
  bırakarak sonlandırıyordu. Döngüler artık yakalanıp geri çekiliyor ve Ctrl-C
  düzgün şekilde çıkıyor.

---

## Proje yapısı

```
main.py                  giriş noktası, pakete yönlendirir
mt5bot/
  cli.py                 run | backtest | doctor
  config.py              TOML + ortam değişkenleri, doğrulanmış
  models.py              Side, Signal, Tick, Position, SymbolSpec, ...
  indicators.py          Wilder RSI, SMA, EMA, ATR — saf fonksiyonlar
  risk.py                pip matematiği, stop yerleşimi, pozisyon boyutlandırma
  engine.py              işlem döngüsü
  backtest.py            tekrar oynatma ve metrikler
  brokers/
    base.py              üstündeki her şeyin yazıldığı arayüz
    mt5.py               canlı adaptör — MetaTrader bilen tek dosya
    paper.py             simüle edilmiş icra, backtest ve kuru çalışma için
  strategy/
    rsi_sma.py           strateji, yapılandırılabilir
```

`brokers/base.py` dikişi, geri kalanı test edilebilir kılan şeydir: MetaTrader
paketi bir CI makinesine kurulamaz ve yukarıdaki hatalar tam olarak ancak bir
testin yakalayabileceği türden hatalardır.

---

## Geliştirme

```bash
pip install -e ".[dev]"

ruff check . && ruff format --check .
mypy mt5bot
pytest
```

Hepsi MetaTrader kurulu olmadan çalışır. CI aynısını Python 3.11, 3.12 ve
3.13'te koşar.

Strateji eklemek için `mt5bot/strategy/base.py` içindeki `Strategy` arayüzünü
uygulayın: kapanmış barlardan oluşan bir çerçeve alın, bir `Evaluation`
döndürün. `mt5bot.indicators` içindekiler üzerine inşa edebileceğiniz saf
fonksiyonlardır ve arayüzü uygulayan her şey, para verilmeden önce backtest
edilebilir.

---

## Risk

**Bu eğitim amaçlı bir yazılımdır. Kaldıraçlı enstrümanlarda işlem yapmak para
kaybettirir.**

* Önce **demo hesapta** test edin; bir zarar serisini, bir hafta sonu boşluğunu
  ve bir bağlantı kopmasını görecek kadar uzun süre.
* Backtest bir hipotezdir, tahmin değil. Bu backtest swap'ı, requote'ları,
  değişken spread'i ve yapılandırdığınız sabit rakamın ötesindeki kaymayı
  yok sayar.
* Örnek veri sentetiktir. Üzerindeki sonuçlar hiçbir şey ifade etmez.
* Bu stratejinin kârlı olduğunu kimse doğrulamadı. Bu README'deki kanıtlar
  aksi yönde.
* Kaybetmeyi göze alamayacağınız parayı asla riske atmayın.

MIT lisanslı — [LICENSE.md](LICENSE.md). Hiçbir garanti verilmez.
