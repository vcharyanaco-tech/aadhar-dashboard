import { Routes, Route, Navigate } from 'react-router-dom';
import LoginCard from './components/auth/LoginCard';
import Dashboard from './components/dashboard/Dashboard';

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<LoginCard />} />
      <Route path="/dashboard" element={<Dashboard />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}