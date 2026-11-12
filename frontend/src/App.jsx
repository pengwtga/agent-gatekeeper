import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from './components/Layout.jsx'
import Playground from './pages/Playground.jsx'
import Assistant from './pages/Assistant.jsx'
import Observability from './pages/Observability.jsx'
import Overview from './pages/observability/Overview.jsx'
import Requests from './pages/observability/Requests.jsx'
import Quality from './pages/observability/Quality.jsx'

// 路由表：左侧栏是外框（Layout），页面是 Outlet 里的内容。
// 可观测是「一栏三项」，用嵌套路由表达层级，而不是三个平级的一级入口。
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route path="/" element={<Navigate to="/assistant" replace />} />
        <Route path="/assistant" element={<Assistant />} />
        <Route path="/playground" element={<Playground />} />

        <Route path="/observability" element={<Observability />}>
          <Route index element={<Overview />} />
          <Route path="requests" element={<Requests />} />
          <Route path="quality" element={<Quality />} />
        </Route>

        <Route path="*" element={<Navigate to="/assistant" replace />} />
      </Route>
    </Routes>
  )
}
