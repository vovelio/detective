import { useState } from "react";
import type { Asset, CardData } from "../contracts";

function ImageBlock({
  code,
  id,
  caption,
  alt,
}: {
  code: string;
  id: string;
  caption: string;
  alt: string;
}) {
  const [failed, setFailed] = useState(false);
  return (
    <figure>
      {failed ? (
        <p className="muted" role="status">
          Изображение временно недоступно.
        </p>
      ) : (
        <img
          src={`/api/v1/rooms/${encodeURIComponent(code)}/assets/${encodeURIComponent(id)}`}
          alt={alt}
          loading="lazy"
          referrerPolicy="no-referrer"
          draggable={false}
          onError={() => setFailed(true)}
        />
      )}
      <figcaption>{caption}</figcaption>
    </figure>
  );
}
export function Card({
  card,
  code,
  assets,
  personal = false,
  children,
}: {
  card: CardData;
  code: string;
  assets: Asset[];
  personal?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <article
      className={`clue-card ${personal ? "personal-card" : "public-card"}`}
      data-card-id={card.id}
      data-testid={personal ? "private-card" : "public-card"}
    >
      <header>
        <span className="eyebrow">
          {personal ? "Личная карта" : "Общая улика"}
        </span>
        <span className="card-number">{card.id}</span>
      </header>
      <h3>{card.title}</h3>
      <div className="card-body">
        {card.blocks.map((block, i) =>
          block.type === "text" ? (
            <p key={i}>
              {block.segments.map((segment, j) =>
                personal && segment.shareable ? (
                  <mark key={j} title="Можно произнести до публикации">
                    {segment.shareable === "clip" ? "📎 " : ""}
                    {segment.text}
                  </mark>
                ) : (
                  <span key={j}>{segment.text}</span>
                ),
              )}
            </p>
          ) : (
            <ImageBlock
              key={`${card.id}-${block.asset_id}-${i}`}
              code={code}
              id={block.asset_id}
              caption={block.caption}
              alt={
                assets.find((asset) => asset.id === block.asset_id)?.alt ??
                block.caption
              }
            />
          ),
        )}
      </div>
      {personal &&
        card.blocks.some(
          (block) =>
            block.type === "text" && block.segments.some((s) => s.shareable),
        ) && (
          <small className="share-note">
            Выделенное можно произнести вслух
          </small>
        )}
      {children && <footer className="card-actions">{children}</footer>}
    </article>
  );
}
