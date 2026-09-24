interface FeatureItemProps {
  iconUrl: string;
  text: string;
}

export default function FeatureItem({ iconUrl, text }: FeatureItemProps) {
  return (
    <div className="flex-1 bg-orange-50 border border-red-100 rounded-[10px] px-2 py-2.5 text-center">
      <img src={iconUrl} alt="" className="h-4 w-4 mx-auto mb-1" />
      <span className="block text-red-900 text-[9.5px] font-bold uppercase tracking-[0.3px]">
        {text}
      </span>
    </div>
  );
}