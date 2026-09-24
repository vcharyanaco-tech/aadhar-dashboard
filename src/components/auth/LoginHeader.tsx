const LOGO_URL =
  'https://c.animaapp.com/ab9rgyp2mxN6827tVLaBtw/assets/image-1.png';

export default function LoginHeader() {
  return (
    <div
      className="relative text-center"
      style={{ background: 'linear-gradient(120deg, rgb(122,31,43), rgb(74,16,23))' }}
    >
      <div className="pt-[34px] pl-[36px] pr-[36px] pb-[46px]">
        <div className="bg-white rounded-xl shadow-lg inline-block px-4 py-[10px] mb-4">
          <img src={LOGO_URL} alt="India Post" className="block w-[150px] h-auto" />
        </div>
        <h1
          className="text-white text-lg leading-[1.35]"
          style={{ fontFamily: 'Cambria, Georgia, serif' }}
        >
          Aadhaar MIS Dashboard
          <br />
          Department of Posts, India
          <br />
          Tamilnadu Circle
        </h1>
      </div>
      <div className="absolute left-0 right-0 bottom-0 h-9 bg-white rounded-t-[50%_100%]" />
    </div>
  );
}