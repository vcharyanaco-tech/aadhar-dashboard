import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import LoginHeader from './LoginHeader';
import LoginForm from './LoginForm';
import FeatureList from './FeatureList';

export default function LoginCard() {
  const navigate = useNavigate();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleLogin = (pin: string) => {
    setLoading(true);
    setError('');
    window.setTimeout(() => {
      if (pin === '1122') {
        localStorage.setItem('isAuthenticated', 'true');
        navigate('/dashboard');
      } else {
        setError('Invalid PIN. Please try again.');
        setLoading(false);
      }
    }, 400);
  };

  return (
    <div className="min-h-screen bg-stone-100 flex items-center justify-center p-6">
      <div
        className="bg-white rounded-[20px] w-full max-w-[460px] overflow-hidden"
        style={{ boxShadow: 'rgba(74,16,23,0.18) 0px 24px 60px' }}
      >
        <LoginHeader />
        <div className="px-9 pt-2 pb-[34px]">
          <LoginForm onLogin={handleLogin} error={error} loading={loading} />
          <FeatureList />
        </div>
      </div>
    </div>
  );
}