# EEG Tabanlı Robotik Kol Kontrol Sistemi 

Bu proje, kullanıcıların beyin dalgalarını (EEG sinyalleri) kullanarak 4 eksenli bir robotik kolu kontrol etmelerini sağlayan donanım ve yazılım entegrasyonu projesidir. Sistem, beyin-bilgisayar arayüzü (BCI) prensiplerini kullanarak insan-makine etkileşiminde verimli bir kontrol mekanizması sunar.

## Proje Hakkında

Sistem, **Emotiv Epoc X** başlığından alınan ham beyin sinyallerini eşik tabanlı (threshold-based) metriklerle işleyerek fiziksel hareket komutlarına dönüştürür. 

Projenin mimarisi, performansı maksimize etmek ve gecikmeyi önlemek üzerine kurulmuştur. Bu nedenle tüm ağır veri işleme ve hesaplama yükü uç cihazda (edge) değil, güçlü bir **Ana İşlem Bilgisayarı (Host Computer)** üzerinde gerçekleştirilir. Hesaplanan kesin kontrol komutları daha sonra bir düğüm (node) olarak çalışan **Raspberry Pi 5**'e iletilir ve 4 eksenli robotik kolun eşzamanlı hareketi sağlanır.

## Öne Çıkan Özellikler

* **Yüksek Performanslı İşlem Mimarisi:** Sinyal analizi ve eşik hesaplamalarının Raspberry Pi yerine güçlü bir ana bilgisayarda yapılması, sistemin tepki süresini ve kararlılığını artırır.
* **Kanıtlanmış Kontrol Verimliliği:** Sistemin kullanılabilirliği ve kontrol hassasiyeti, Kocaeli Üniversitesi insan-bilgisayar etkileşimi laboratuvarında **16 farklı insan denek** ile yapılan kapsamlı bir kullanıcı çalışmasıyla test edilmiş ve doğrulanmıştır.
* **Gelişmiş Donanım Entegrasyonu:** Endüstri standardı EEG donanımı ile mikrobilgisayar tabanlı robotik kontrolün stabil iletişimi.

## Donanım ve Sistem Gereksinimleri

* **EEG Cihazı:** Emotiv Epoc X
* **Ana İşlem Birimi:** Host PC (Hesaplama ve sinyal işleme için)
* **Kontrol Birimi:** Raspberry Pi 5
* **Actuator:** Robotik Kol Modeli
