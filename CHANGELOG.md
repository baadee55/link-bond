# 更新履歴 / Changelog

## 1.5.0
- 箱を新しいデザインに：一番上に状態と「最大の速さ（全回線の合計）」、その下に「速さを測る」、回線ごとのカード、スイッチ / new design
- 意味のない「いまの速さ 0」はやめて、「✓ 応答 32ms・12秒前」でつながっていることを示す / shows that each link is alive instead of 0 Mbps
- 「詳しく」はタブ（グラフ・ダウンロード・残り容量・出来事）にして、画面に収まるように / tabs, the box fits the screen
- Chrome の拡張機能を入れると、ダウンロードが自動で全回線に / with the extension every Chrome download uses all links
- 「節約モード」を「低画質モード」に名前変更 / eco mode renamed to low-quality video mode

## 1.4.2
- 空の行（VPN・お知らせなど）は隠して、箱を中身の大きさぴったりに / empty rows are hidden, the box fits its content

## 1.4.1
- 更新したら、小さな箱に「🆕 新しい版にしました」と出て、押すと変更点が見える / the box shows what's new after an update
- 箱の下に残っていた空白を詰めた / no more empty space at the bottom of the box

## 1.4.0
- 新しい版を置くと、link-bond と小さな箱が自動で入れ替わり、変更点を通知 / new versions are picked up by themselves and announced
- 公開版では、新しい版が出たら 1 日 1 回知らせる / published builds check for a newer release once a day

## 1.3.0
- 実力（最大の速さ）は、ふだんの通信から学ぶ（測定の通信量 0） / capacity is learned from real traffic
- 回線の調子（応答時間・取りこぼし）を小さな問い合わせで表示、定期の速度測定はやめて「測る」ボタンに / cheap condition checks, speed test on demand
- 問題があれば小さい表示は全体が赤、大きい表示は問題の所だけ赤 / red pill on any problem, red only where the problem is
- 速さは Windows のアダプタの実際の数値から（PC 全体の本当の速さ） / real speed from the adapter counters

## 1.2.0
- 小さな浮かぶ箱（動かせる・半透明・最小化）、全回線で高速ダウンロード、切れても残りの回線で続ける / floating widget, fast download, failover
- つないだ回線は全部、常に同時に、速さに比例して使う / every link used all the time, in proportion to its speed

## 1.1.0
- 節約モード、プリペイドの残り、広告ブロック、ダッシュボード、毎日の報告 / eco mode, prepaid balance, ad blocking, dashboard, daily report

## 1.0.0
- 最初の版：複数の回線を同時に使うローカルプロキシ、ON/OFF、落ちても安全 / first release
