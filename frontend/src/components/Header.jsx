/**
 * 页面标题区。
 *
 * 导航搬到左侧栏之后，这里只剩下「这一页是干什么的」。
 * 不再用渐变大色块：一页一个渐变块在侧栏布局里会跟导航抢视线，
 * 且每切一次页就闪一次，值班时反而干扰。
 * children 用来塞页面自己的状态徽标（如知识库条数、数据更新时间）。
 */
export default function Header({ title, desc, children }) {
  return (
    <header className="page-head">
      <div>
        <h1>{title}</h1>
        {desc && <p>{desc}</p>}
        {children}
      </div>
    </header>
  )
}
