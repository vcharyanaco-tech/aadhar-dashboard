import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  BarChart3,
  Building2,
  Calendar,
  Download,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  LogOut,
  MapPin,
  Menu,
  TrendingUp,
  Users,
  X,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

const LOGO_URL = 'https://c.animaapp.com/ab9rgyp2mxN6827tVLaBtw/assets/image-1.png';

interface Report {
  id: number;
  name: string;
  icon: LucideIcon;
  description: string;
}

const REPORTS: Report[] = [
  { id: 1, name: 'Division Wise Summary', icon: Building2, description: 'Summary of Aadhaar enrollments by division' },
  { id: 2, name: 'Sub Division Report', icon: MapPin, description: 'Detailed sub-division level statistics' },
  { id: 3, name: 'Head Post Office Report', icon: FileText, description: 'HPO-wise enrollment data' },
  { id: 4, name: 'Sub Post Office Report', icon: FileText, description: 'SPO-wise enrollment data' },
  { id: 5, name: 'Branch Post Office Report', icon: FileText, description: 'BPO-wise enrollment data' },
  { id: 6, name: 'Operator Performance', icon: Users, description: 'Individual operator statistics' },
  { id: 7, name: 'Daily Transaction Report', icon: Calendar, description: 'Day-wise transaction summary' },
  { id: 8, name: 'Monthly Progress Report', icon: BarChart3, description: 'Month-wise progress tracking' },
  { id: 9, name: 'Quarterly Analysis', icon: TrendingUp, description: 'Quarter-wise trend analysis' },
  { id: 10, name: 'Rejection Analysis', icon: FileText, description: 'Analysis of rejected applications' },
  { id: 11, name: 'Pending Applications', icon: FileText, description: 'Status of pending applications' },
];

const STATS = [
  { label: 'Total Enrollments', value: '12,45,678', valueClass: 'text-rose-900', note: '+12.5% from last month', noteClass: 'text-green-600' },
  { label: "Today's Transactions", value: '3,245', valueClass: 'text-amber-600', note: '+8.2% from yesterday', noteClass: 'text-green-600' },
  { label: 'Active Operators', value: '567', valueClass: 'text-blue-600', note: 'Across all divisions', noteClass: 'text-gray-500' },
  { label: 'Pending Applications', value: '1,234', valueClass: 'text-red-600', note: 'Requires attention', noteClass: 'text-red-600' },
];

const ACCESS_LEVELS = [
  { name: 'Circle Admin', sub: 'Full Access', color: 'bg-red-800' },
  { name: 'Regional Admin', sub: 'Regional Data', color: 'bg-amber-600' },
  { name: 'Division Admin', sub: 'Division Data', color: 'bg-blue-600' },
  { name: 'Operator', sub: 'View Only', color: 'bg-green-600' },
];

const TABLE_ROWS = [
  { division: 'Chennai City', total: '245678', today: '456', month: '12345', pending: '123' },
  { division: 'Chennai Central', total: '198765', today: '389', month: '9876', pending: '89' },
  { division: 'Coimbatore', total: '156432', today: '298', month: '8765', pending: '67' },
  { division: 'Madurai', total: '134567', today: '267', month: '7654', pending: '45' },
  { division: 'Tiruchirappalli', total: '112345', today: '234', month: '6543', pending: '34' },
  { division: 'Salem', total: '98765', today: '198', month: '5432', pending: '28' },
  { division: 'Tirunelveli', total: '87654', today: '176', month: '4321', pending: '23' },
  { division: 'Vellore', total: '76543', today: '156', month: '3456', pending: '19' },
];

export default function Dashboard() {
  const navigate = useNavigate();
  const [selectedReport, setSelectedReport] = useState<number | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    if (!localStorage.getItem('isAuthenticated')) {
      navigate('/', { replace: true });
    }
  }, [navigate]);

  const handleLogout = () => {
    localStorage.removeItem('isAuthenticated');
    navigate('/', { replace: true });
  };

  const handleExport = (format: string) => {
    window.alert(`Exporting report in ${format} format...`);
  };

  const activeReport = REPORTS.find((report) => report.id === selectedReport) ?? null;

  return (
    <div className="min-h-screen bg-stone-100 flex">
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={`fixed lg:static inset-y-0 left-0 z-50 w-72 bg-gradient-to-b from-rose-900 to-rose-950 text-white transform transition-transform duration-300 flex flex-col ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        } lg:translate-x-0`}
      >
        <div className="p-4 border-b border-rose-800">
          <div className="flex items-center justify-between">
            <div className="bg-white rounded-lg p-2">
              <img src={LOGO_URL} alt="India Post" className="w-24" />
            </div>
            <button
              className="lg:hidden text-white"
              onClick={() => setSidebarOpen(false)}
              aria-label="Close sidebar"
            >
              <X size={24} />
            </button>
          </div>
          <h2 className="mt-3 text-sm font-bold">Aadhaar MIS Dashboard</h2>
          <p className="text-xs text-rose-300">Tamilnadu Circle</p>
        </div>

        <nav className="p-4 flex-1 overflow-y-auto max-h-[calc(100vh-200px)]">
          <p className="text-xs text-rose-400 uppercase tracking-wider mb-3">Reports</p>
          <ul className="space-y-1">
            {REPORTS.map((report) => {
              const Icon = report.icon;
              const active = report.id === selectedReport;
              return (
                <li key={report.id}>
                  <button
                    onClick={() => {
                      setSelectedReport(report.id);
                      setSidebarOpen(false);
                    }}
                    className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
                      active
                        ? 'bg-white/20 text-white'
                        : 'text-rose-200 hover:bg-white/10 hover:text-white'
                    }`}
                  >
                    <Icon size={18} />
                    <span className="truncate">{report.name}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="p-4 border-t border-rose-800">
          <button
            onClick={handleLogout}
            className="w-full flex items-center justify-center gap-2 px-4 py-2 bg-rose-800 hover:bg-rose-700 rounded-lg text-sm transition-colors"
          >
            <LogOut size={18} />
            Logout
          </button>
        </div>
      </aside>

      <main className="flex-1 flex flex-col min-h-screen">
        <header className="bg-white shadow-sm px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <button
              className="lg:hidden text-gray-600"
              onClick={() => setSidebarOpen(true)}
              aria-label="Open sidebar"
            >
              <Menu size={24} />
            </button>
            <h1 className="text-lg font-bold text-rose-900">
              {activeReport ? activeReport.name : 'Welcome to Aadhaar MIS Dashboard'}
            </h1>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => handleExport('Excel')}
              className="flex items-center gap-1 px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white text-sm rounded-lg transition-colors"
            >
              <FileSpreadsheet size={16} />
              <span className="hidden sm:inline">Excel</span>
            </button>
            <button
              onClick={() => handleExport('PDF')}
              className="flex items-center gap-1 px-3 py-1.5 bg-red-600 hover:bg-red-700 text-white text-sm rounded-lg transition-colors"
            >
              <Download size={16} />
              <span className="hidden sm:inline">PDF</span>
            </button>
            <button
              onClick={() => handleExport('PNG')}
              className="flex items-center gap-1 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors"
            >
              <ImageIcon size={16} />
              <span className="hidden sm:inline">PNG</span>
            </button>
          </div>
        </header>

        <div className="flex-1 p-4 lg:p-6 overflow-y-auto">
          {activeReport ? (
            <ReportTable report={activeReport} />
          ) : (
            <HomeView onSelectReport={setSelectedReport} />
          )}
        </div>

        <footer className="bg-white border-t px-4 py-3 text-center text-xs text-gray-500">
          © 2024 Department of Posts, India - Tamilnadu Circle | Aadhaar MIS Dashboard
        </footer>
      </main>
    </div>
  );
}

function HomeView({ onSelectReport }: { onSelectReport: (id: number) => void }) {
  return (
    <>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        {STATS.map((stat) => (
          <div key={stat.label} className="bg-white rounded-xl p-4 shadow-sm">
            <p className="text-xs text-gray-500 uppercase">{stat.label}</p>
            <p className={`text-2xl font-bold ${stat.valueClass}`}>{stat.value}</p>
            <p className={`text-xs ${stat.noteClass}`}>{stat.note}</p>
          </div>
        ))}
      </div>

      <div className="bg-white rounded-xl p-4 shadow-sm mb-6">
        <h3 className="text-sm font-bold text-gray-800 mb-4">Access Levels</h3>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {ACCESS_LEVELS.map((level) => (
            <div key={level.name} className="border rounded-lg p-3">
              <div
                className={`w-8 h-8 ${level.color} rounded-lg flex items-center justify-center mb-2`}
              >
                <Users size={16} className="text-white" />
              </div>
              <p className="font-medium text-sm">{level.name}</p>
              <p className="text-xs text-gray-500">{level.sub}</p>
            </div>
          ))}
        </div>
      </div>

      <div className="bg-white rounded-xl p-4 shadow-sm">
        <h3 className="text-sm font-bold text-gray-800 mb-4">Quick Access - Reports</h3>
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
          {REPORTS.slice(0, 8).map((report) => {
            const Icon = report.icon;
            return (
              <button
                key={report.id}
                onClick={() => onSelectReport(report.id)}
                className="border rounded-lg p-3 text-left hover:bg-rose-50 hover:border-rose-200 transition-colors"
              >
                <Icon size={20} className="text-rose-800 mb-2" />
                <p className="font-medium text-sm text-gray-800">{report.name}</p>
                <p className="text-xs text-gray-500 line-clamp-2">{report.description}</p>
              </button>
            );
          })}
        </div>
      </div>
    </>
  );
}

function ReportTable({ report }: { report: Report }) {
  return (
    <div className="bg-white rounded-xl shadow-sm overflow-hidden">
      <div className="px-4 py-3 border-b">
        <h3 className="font-bold text-gray-800">{report.name}</h3>
        <p className="text-sm text-gray-500">{report.description}</p>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-rose-900 text-white">
              <th className="px-4 py-3 text-left">S.No</th>
              <th className="px-4 py-3 text-left">Division/Unit</th>
              <th className="px-4 py-3 text-right">Total Enrollments</th>
              <th className="px-4 py-3 text-right">Today</th>
              <th className="px-4 py-3 text-right">This Month</th>
              <th className="px-4 py-3 text-right">Pending</th>
              <th className="px-4 py-3 text-left">Status</th>
            </tr>
          </thead>
          <tbody>
            {TABLE_ROWS.map((row, index) => (
              <tr key={row.division} className="hover:bg-gray-50 border-t border-gray-100">
                <td className="px-4 py-3">{index + 1}</td>
                <td className="px-4 py-3 font-medium">{row.division}</td>
                <td className="px-4 py-3 text-right">{row.total}</td>
                <td className="px-4 py-3 text-right text-green-600">{row.today}</td>
                <td className="px-4 py-3 text-right">{row.month}</td>
                <td className="px-4 py-3 text-right text-amber-600">{row.pending}</td>
                <td className="px-4 py-3">
                  <span className="bg-green-100 text-green-700 text-xs rounded-full px-2 py-1">
                    Active
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="p-4 border-t bg-gray-50 flex items-center justify-between">
        <p className="text-sm text-gray-500">Showing 8 of 23 divisions</p>
        <div className="flex gap-2">
          <button className="px-3 py-1.5 border rounded-lg text-sm text-gray-600">
            Previous
          </button>
          <button className="px-3 py-1.5 bg-rose-800 text-white rounded-lg text-sm">Next</button>
        </div>
      </div>
    </div>
  );
}