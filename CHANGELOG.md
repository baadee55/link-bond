# 更新履歴 / Changelog

## 1.5.1
- **不安定な回線のための作り直し** / rebuilt for unstable links:
- 名前（DNS）を全部の回線で同時に引く。Windows の回線が切れても名前が引け、何も答えない時は前に引いた住所を使う / names are looked up over every link at once, with a last-known fallback
- つながらない回線は 0.3 秒ほどで見切って次の回線と並べてつなぐ（前は 4 秒待っていた） / a dead link costs ~0.3 s, not 4 s
- 切れた回線は約 8 秒で外し（前は約 45 秒）、5 秒ごとに確かめて戻ればすぐ使う。戻った直後と、よく切れる回線は控えめに使う / cuts found in ~8 s, links back in seconds, flaky links get less
- 全部の回線が一瞬切れても、新しい接続は最大 15 秒待って、戻った回線でつなぐ（すぐエラーにしない） / connections wait up to 15 s for a link to come back
- 20 秒以内の瞬断は通知しない（記録には残す）。本当に切れた時と戻った時だけ知らせる / short blips are recorded, not notified
- ダウンロードは、全部の回線が切れても途中で止めて待ち、戻ったら続きから。途中で戻った回線もすぐ使う / downloads pause through outages and use links that come back
- 回線ごとに「今日何回切れたか・何 % つながっていたか」を箱と毎日のお知らせに / drops and uptime per link, in the box and the daily report
- **接続の記録のページ**（箱の「接続の記録」・ http://127.0.0.1:1081/history ）：回線ごとに 1 日のどこで切れたか、応答時間、全部切れていた時間、ネットの代わりに返ってきたページ（データ切れ等）、出来事 / connection record page
- **自動復旧**（install-repair.ps1 を 1 回実行）：応答しないまま 90 秒の回線・アドレスが来ないアダプタ・つながらない Wi-Fi を、そのアダプタだけ入れ直してつなぎ直す。iPhone が名前を隠してもホットスポットを探しに行く / auto-repair of stuck adapters, hidden hotspots found
- 見張りが「link-bond が何も通さない」状態を見つけたら、プロキシを外して立て直す（ネットが止まらない） / fail-open: if link-bond carries nothing, the proxy is removed
- 新しい版が出たら自動で入れ替わる（6 時間ごとに確認・箱の「更新する」ですぐにも）。1.5.0 までは確認自体が失敗していた / updates install themselves (the check never worked before)
- 回線をつなぎ直したとき「実力 17000 Mbps」のような、ありえない速さを覚えていたのを直した / fixed impossible link speeds after a reconnect
- 切れたスマホの Wi-Fi に自分でつなぎ直し続ける（Windows はすぐあきらめるため） / rejoins a dropped phone hotspot by itself
- 接続状況を 15 秒ごとに全部記録（connlog フォルダ・14 日分） / a full connection record, kept 14 days
- 新しい版が起動直後に落ち続けたら、自動で元の版に戻す / a failing update is rolled back by itself
- 止まった（固まった）link-bond も見張りが見つけて立て直す / a hung link-bond is restarted too
- スマホをつないだのにテザリングが止まっている時も、すぐ通知 / tells at once when a phone is plugged in but not tethering
- 小さな箱が画面（タスクバー）からはみ出さない・✕ は隠すだけ・消えても自動で戻る・二重に開かない / the box stays on screen, ✕ hides it, it comes back, never twice
- 「詳しい画面」の説明を分かりやすく / clearer note about the dashboard

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
